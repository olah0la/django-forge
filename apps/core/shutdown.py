"""Graceful shutdown — what the process does between SIGTERM and exit.

A container is stopped on every deploy, every scale event and every node
replacement. The platform sends `SIGTERM`, waits a grace period, then
`SIGKILL`s. An application that ignores `SIGTERM` drops every in-flight request
on every release — errors that correlate with deploys and are hard to attribute
because nothing in the application logs them.

The sequence this module implements, in order:

    1. SIGTERM arrives at the server process (PID 1 — docker-entrypoint.sh
       `exec`s, so no shell absorbs it)
    2. readiness starts answering 503, so the platform routes no NEW traffic in
    3. the server stops accepting connections and finishes what is in flight
    4. database connections are closed
    5. the process exits, well inside the platform's grace period

Steps 1, 3 and 5 belong to uvicorn and gunicorn and already work. This module
owns 2 and 4, and docs/ops.md walks the whole thing through.

**Why the signal handler is wrapped rather than simply installed.** Step 2 has
to happen the INSTANT the signal lands. The obvious hook — ASGI lifespan
shutdown — is far too late: uvicorn sends `lifespan.shutdown` at the very END of
`Server.shutdown()`, after listeners are closed and in-flight requests have
already drained (verified against uvicorn 0.38, `uvicorn/server.py`). Readiness
flipping there would flip after the drain it was supposed to announce.

What makes the wrapping work is an ordering that is worth knowing before
changing it: `Server.serve()` installs uvicorn's own SIGTERM handler in
`capture_signals()` and only THEN runs lifespan startup. So by the time
`lifespan.startup` reaches us, `signal.getsignal(SIGTERM)` is uvicorn's handler
and we can put ourselves in front of it. Under gunicorn the same holds, because
each worker runs its own `Server.serve()`.

**Delegation is not optional.** A handler that sets the flag and does not call
the one it replaced swallows the signal: uvicorn never learns to stop, the
platform waits out the full grace period, and then SIGKILLs — every in-flight
request dropped, which is worse than having no drain at all.
"""

import logging
import signal
import threading
from collections.abc import Awaitable, Callable
from types import FrameType
from typing import Any

from asgiref.sync import sync_to_async

logger = logging.getLogger(__name__)

# An Event rather than a bool: it is set from a signal handler and read from
# request threads, and Event's set/is_set are the ones documented to be safe
# across both.
_shutting_down = threading.Event()

# Signal handlers are process-wide, so installing twice would chain this module
# in front of itself and log the shutdown line twice.
_handlers_installed = threading.Event()

# SIGINT as well as SIGTERM: Ctrl-C in development should drain the same way a
# deploy does, or the graceful path is only ever exercised in production.
_HANDLED_SIGNALS = (signal.SIGTERM, signal.SIGINT)


def is_shutting_down() -> bool:
    """Has shutdown begun? Read by the readiness probe (apps/core/health.py)."""
    return _shutting_down.is_set()


def begin_shutdown() -> None:
    """Mark the process as draining. Idempotent, and safe from a signal handler.

    Deliberately does almost nothing. This runs INSIDE a signal handler, which
    interrupts whatever the main thread was doing — including, possibly, a
    request being served. Anything slow here (a database write, a network call,
    an `atexit`-style flush) stalls the very requests the drain exists to let
    finish, and re-entrant logging is the classic way to deadlock a process at
    exactly the moment it is trying to leave.
    """
    if _shutting_down.is_set():
        return
    _shutting_down.set()

    # WARNING, not INFO: this line is the anchor an operator uses to tell a
    # clean drain apart from a crash when reading back a deploy, and it should
    # survive a log level raised to quieten a noisy service.
    logger.warning("shutdown: SIGTERM received, draining — readiness now reports not ready")


def install_signal_handlers() -> None:
    """Put `begin_shutdown()` in front of whatever handler is already installed.

    Called from ASGI lifespan startup, which is the one moment the server's own
    handlers are guaranteed to be in place — see this module's docstring.
    """
    if _handlers_installed.is_set():
        return

    # `signal.signal` raises ValueError off the main thread. That is not a
    # failure worth propagating: it means something is hosting the application
    # in a way where the main thread owns signals, and the right behaviour is to
    # leave that alone rather than refuse to start.
    if threading.current_thread() is not threading.main_thread():
        logger.debug("shutdown: not the main thread, leaving signal handlers alone")
        return

    for sig in _HANDLED_SIGNALS:
        previous = signal.getsignal(sig)
        signal.signal(sig, _make_handler(sig, previous))

    _handlers_installed.set()


def _make_handler(sig: int, previous: Any) -> Callable[[int, FrameType | None], None]:
    """Build a handler that marks the drain, then defers to `previous`."""

    def handler(signum: int, frame: FrameType | None) -> None:
        begin_shutdown()

        if callable(previous):
            previous(signum, frame)
            return

        # SIG_DFL or SIG_IGN — nothing to call. Restore that disposition and
        # re-raise, so the process ends up doing exactly what it would have done
        # without us. Without this, a default-disposition SIGTERM would be
        # swallowed and the container would hang until it was SIGKILLed.
        signal.signal(sig, previous)
        signal.raise_signal(signum)

    return handler


def close_database_connections() -> None:
    """Close this process's database connections on the way out.

    Load-bearing because CONN_MAX_AGE is 60s (config/settings/base.py):
    connections are held open for reuse rather than closed per request, so a
    worker that exits without this leaves PostgreSQL to notice the dropped
    socket and reap the backend itself. Multiply by the worker count on every
    deploy and a rolling restart can hold two generations' connections at once —
    against a max_connections the arithmetic in docs/serving.md already shows is
    the binding constraint.

    Imported here rather than at module import: config/gunicorn.py calls this
    from a hook and must stay importable before Django is configured.
    """
    from django.db import connections

    connections.close_all()
    logger.info("shutdown: database connections closed")


class ShutdownLifespanMiddleware:
    """ASGI wrapper adding a lifespan handler to an application that has none.

    Django's `ASGIHandler` serves `http` and rejects every other scope type, so
    uvicorn detects lifespan as unsupported and disables it. Wrapping it here is
    what gives the process a startup and shutdown hook at all.

    The lifespan protocol is TERMINATED here, not forwarded: passing it down
    would reach Django's handler and raise. Every other scope goes straight
    through untouched, so this is transparent to request handling.
    """

    def __init__(self, app: Callable[..., Awaitable[None]]) -> None:
        self.app = app

    async def __call__(
        self,
        scope: dict[str, Any],
        receive: Callable[[], Awaitable[dict[str, Any]]],
        send: Callable[[dict[str, Any]], Awaitable[None]],
    ) -> None:
        if scope["type"] != "lifespan":
            await self.app(scope, receive, send)
            return

        while True:
            message = await receive()

            if message["type"] == "lifespan.startup":
                try:
                    install_signal_handlers()
                except Exception as exc:  # pragma: no cover - defensive
                    # Reported rather than raised: a server that refuses to
                    # start because it could not arrange its own shutdown is a
                    # worse outcome than one that starts without a drain.
                    logger.exception("shutdown: could not install signal handlers")
                    await send({"type": "lifespan.startup.failed", "message": str(exc)})
                    return
                await send({"type": "lifespan.startup.complete"})

            elif message["type"] == "lifespan.shutdown":
                # By the time this arrives the in-flight requests have already
                # finished — this is the end of the drain, not the start of it,
                # which is why the readiness flip lives in the signal handler
                # instead. Closing connections IS correct here: it must happen
                # after the last request that might use one.
                #
                # In a THREAD, not on the event loop. Django decorates
                # `connection.close()` with @async_unsafe, so calling it from
                # here raises SynchronousOnlyOperation — and connections are
                # thread-local, held by the same executor thread that
                # `sync_to_async(thread_sensitive=True)` runs sync views in, so
                # the loop thread could not see them to close them anyway.
                #
                # Found by running it: uvicorn reports an exception raised here
                # as "ASGI 'lifespan' protocol appears unsupported", at INFO,
                # with no traceback — so this failed silently and looked like a
                # server that simply had no lifespan support.
                try:
                    await sync_to_async(close_database_connections, thread_sensitive=True)()
                except Exception as exc:
                    # Reported, not raised, and NOT as a lifespan failure: the
                    # process is leaving either way, and a connection this
                    # process failed to close is one the database will reap.
                    # Losing the reason is what made this hard to find.
                    logger.exception("shutdown: closing database connections failed: %s", exc)

                await send({"type": "lifespan.shutdown.complete"})
                return
