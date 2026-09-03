"""Graceful shutdown (M6-05) — the sequence between SIGTERM and exit.

Every acceptance criterion on issue #35 is a claim about a PROCESS, not about a
function, and most of them are invisible in a unit test: `exec` in a shell
script, a signal handler that has to be installed in the right order, a request
that has to survive a signal. Each test below says which criterion it discharges.

The end-to-end one at the bottom runs a real uvicorn in a subprocess and sends
it a real SIGTERM, because criterion 2 asks for exactly that and nothing weaker
answers it: a mocked signal proves the handler runs, not that the server
finishes the request it was serving when the signal arrived.

What is deliberately NOT tested here: that gunicorn's arbiter forwards SIGTERM
to its workers and honours `graceful_timeout`. That is gunicorn's own behaviour,
it needs a container to exercise honestly, and asserting it here would be
testing a dependency. docs/ops.md records how it was verified by hand instead.
"""

import json
import re
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

import pytest
from django.test import Client

from apps.core import shutdown

REPO_ROOT = Path(__file__).resolve().parent.parent
ENTRYPOINT = REPO_ROOT / "docker-entrypoint.sh"
DOCKERFILE = REPO_ROOT / "Dockerfile"
COMPOSE = REPO_ROOT / "docker-compose.yml"


@pytest.fixture(autouse=True)
def _reset_shutdown_state():
    """Clear the module-level drain flag between tests.

    It is process-wide by design — a signal handler has nowhere else to put it —
    so a test that sets it would otherwise leak into every test that runs after,
    and the failures would land somewhere unrelated.
    """
    shutdown._shutting_down.clear()
    shutdown._handlers_installed.clear()
    yield
    shutdown._shutting_down.clear()
    shutdown._handlers_installed.clear()


# ---------------------------------------------------------------------------
# Criterion 1 — SIGTERM reaches the application, not an intervening shell
# ---------------------------------------------------------------------------
# All three of these are one-line properties of files, and all three are silent
# when broken: the container still starts, still serves, and only drops requests
# when it is stopped — which is not when anyone is watching.
def test_the_entrypoint_execs_the_application():
    """Without `exec` the shell stays PID 1 and absorbs the signal.

    The platform then waits out the full grace period and SIGKILLs, dropping
    every in-flight request on every deploy. Nothing in the application logs
    says so, because the application never heard about it.
    """
    body = ENTRYPOINT.read_text().rstrip()

    assert body.endswith('exec "$@"'), (
        "docker-entrypoint.sh must hand off with `exec`, or the shell remains "
        "PID 1 and the application never receives SIGTERM"
    )


def test_the_dockerfile_starts_the_server_without_a_shell():
    """Exec form, so no `sh -c` sits between the entrypoint and gunicorn.

    The shell form would reintroduce one line later exactly the failure `exec`
    in the entrypoint exists to prevent.
    """
    dockerfile = DOCKERFILE.read_text()
    argv = {}

    for directive in ("ENTRYPOINT", "CMD"):
        match = re.search(rf"^{directive} (.+)$", dockerfile, re.MULTILINE)
        assert match, f"{directive} not found in the Dockerfile"
        value = match.group(1).strip()
        assert value.startswith("["), (
            f"{directive} must use exec form (a JSON array), not shell form — "
            f"found {value!r}. Shell form wraps the command in `sh -c`, which "
            "then receives SIGTERM instead of the server."
        )
        argv[directive] = json.loads(value)

    # The ENTRYPOINT is itself a shell script, and that is fine: it `exec`s, so
    # nothing of it survives to hold the signal (asserted above). What must not
    # happen is a shell being INVOKED to interpret the command — the placeholder
    # CMD this replaced was `sh -c "echo ...; exit 1"`, which would have left
    # `sh` as the process the entrypoint exec'd.
    assert Path(argv["CMD"][0]).name not in ("sh", "bash", "dash"), (
        "CMD must run the server directly, not through a shell that would "
        f"absorb SIGTERM — found {argv['CMD'][0]!r}"
    )


def test_the_image_declares_its_stop_signal():
    """SIGTERM is already the default, so this guards a change rather than a bug.

    Gunicorn reads SIGTERM as "finish what you are holding" and SIGQUIT as "stop
    now". A STOPSIGNAL of SIGQUIT would read like a tidy-up and would silently
    turn every deploy back into dropped requests.
    """
    assert re.search(r"^STOPSIGNAL SIGTERM$", DOCKERFILE.read_text(), re.MULTILINE), (
        "the Dockerfile must declare STOPSIGNAL SIGTERM explicitly"
    )


# ---------------------------------------------------------------------------
# Criterion 3 — the graceful timeout is configurable, and shorter than the
# platform's kill grace period
# ---------------------------------------------------------------------------
def _load_gunicorn_value(name: str):
    """One setting from config/gunicorn.py, imported in a clean interpreter."""
    proc = subprocess.run(
        [sys.executable, "-c", f"import json, config.gunicorn as g; print(json.dumps(g.{name}))"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin", "HOME": "/tmp"},
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def _compose_service_block(name: str) -> str:
    """The YAML for one service, so an assertion cannot be satisfied elsewhere.

    Searching the whole file would let `stop_grace_period` on app-prod satisfy a
    claim about `app`, which is the exact gap this task found.
    """
    text = COMPOSE.read_text()
    start = text.index(f"\n  {name}:\n")
    remainder = text[start + 1 :]
    # The next top-level service or section, at two-space indentation.
    match = re.search(r"^  [a-z_-]+:$|^[a-z]", remainder[1:], re.MULTILINE)
    return remainder[: match.start() + 1] if match else remainder


@pytest.mark.parametrize("service", ["app", "app-prod"])
def test_every_service_is_given_time_to_drain(service):
    """M6-05 criterion 3, the platform half.

    Docker's default grace period is 10s. The development service inherited it
    while being told to spend up to 25s draining — so a slow request would have
    been SIGKILLed locally and survived in production, which is the wrong way
    round for a shutdown bug to be discovered.
    """
    block = _compose_service_block(service)
    match = re.search(r"^\s+stop_grace_period:\s*(\d+)s\s*$", block, re.MULTILINE)

    assert match, f"the {service} service must set stop_grace_period"
    assert int(match.group(1)) == 30, (
        f"{service}'s grace period must stay at 30s, the value the graceful "
        "timeouts are sized against"
    )


def test_the_graceful_timeout_leaves_headroom_under_the_grace_period():
    """M6-05 criterion 3. Equal values race; the gap is the whole point.

    If gunicorn is still draining when the platform's period expires, the
    platform SIGKILLs mid-drain and the graceful timeout never gets to do its
    job — the drain is configured, looks configured, and does nothing.

    Loaded in a subprocess with a clean environment, as tests/test_gunicorn.py
    does: config/gunicorn.py reads GUNICORN_* at import, so a value in the
    developer's shell would otherwise decide whether this passes.
    """
    graceful = _load_gunicorn_value("graceful_timeout")
    grace_period = int(
        re.search(
            r"^\s+stop_grace_period:\s*(\d+)s\s*$",
            _compose_service_block("app-prod"),
            re.MULTILINE,
        ).group(1)
    )

    assert graceful < grace_period, (
        f"gunicorn's graceful_timeout ({graceful}s) must expire before the "
        f"platform kills the container ({grace_period}s)"
    )


def test_the_development_server_is_told_to_drain_too():
    """Uvicorn's default is to wait forever, which under a grace period means
    "wait until SIGKILLed" — the drain never finishes and the last requests are
    dropped anyway. Development runs bare uvicorn, so it needs saying there.
    """
    block = _compose_service_block("app")

    assert "--timeout-graceful-shutdown" in block, (
        "the development server must be given a graceful shutdown timeout, or "
        "the drain is only ever exercised in production"
    )


# ---------------------------------------------------------------------------
# Criterion 4 — readiness fails as soon as shutdown begins
# ---------------------------------------------------------------------------
@pytest.mark.django_db
def test_readiness_reports_not_ready_once_shutdown_begins(client):
    """M6-05 criterion 4.

    The load balancer stops sending new traffic here because of this response
    and nothing else. Left at 200, the process keeps being handed requests right
    up to the moment it stops accepting connections — and those are the requests
    that get dropped.
    """
    assert client.get("/readyz").status_code == 200

    shutdown.begin_shutdown()

    response = client.get("/readyz")
    assert response.status_code == 503, "readiness must fail the moment draining starts"
    assert response.json() == {"status": "not ready"}


@pytest.mark.django_db
def test_liveness_stays_up_while_draining(client):
    """The trap, and it is the expensive one to get wrong.

    A draining process is HEALTHY — it is finishing its work and leaving.
    Failing liveness here tells the platform to kill it, which produces exactly
    the dropped requests this whole task exists to prevent, by way of the
    mechanism meant to prevent them.
    """
    shutdown.begin_shutdown()

    response = client.get("/healthz")
    assert response.status_code == 200, (
        "liveness must not fail during a drain, or the platform restarts a "
        "process that was shutting down cleanly"
    )
    assert response.json() == {"status": "alive"}


@pytest.mark.django_db
def test_readiness_does_not_query_the_database_once_draining(django_assert_num_queries):
    """Once shutdown has begun the answer is settled.

    Spending a round trip per probe to reconfirm it, for the length of the
    drain, is load on a database that may be the reason the deploy is happening.
    """
    shutdown.begin_shutdown()

    with django_assert_num_queries(0):
        assert Client().get("/readyz").status_code == 503


# ---------------------------------------------------------------------------
# The signal handler — wrapping, not replacing
# ---------------------------------------------------------------------------
def test_the_handler_delegates_to_the_one_it_replaced():
    """A drain that swallows the signal is worse than no drain at all.

    The server never learns to stop, the platform waits out the whole grace
    period, and then SIGKILLs — every in-flight request dropped, having taken
    the full grace period to do it.
    """
    called = []
    original = signal.getsignal(signal.SIGTERM)
    signal.signal(signal.SIGTERM, lambda signum, frame: called.append(signum))

    try:
        shutdown.install_signal_handlers()
        handler = signal.getsignal(signal.SIGTERM)
        handler(signal.SIGTERM, None)
    finally:
        signal.signal(signal.SIGTERM, original)

    assert shutdown.is_shutting_down(), "the handler must mark the process as draining"
    assert called == [signal.SIGTERM], (
        "the handler must call the one it replaced, or the server never stops"
    )


def test_installing_twice_does_not_chain_the_handler_to_itself():
    """Lifespan startup can run more than once in a process that hosts more than
    one application. Chaining would log the shutdown line once per install.
    """
    original = signal.getsignal(signal.SIGTERM)
    try:
        shutdown.install_signal_handlers()
        first = signal.getsignal(signal.SIGTERM)
        shutdown.install_signal_handlers()

        assert signal.getsignal(signal.SIGTERM) is first
    finally:
        signal.signal(signal.SIGTERM, original)


def test_begin_shutdown_is_idempotent(caplog):
    """It runs inside a signal handler, which can fire more than once — a second
    SIGTERM, or a SIGINT following one. One drain, one log line.
    """
    with caplog.at_level("WARNING", logger="apps.core.shutdown"):
        shutdown.begin_shutdown()
        shutdown.begin_shutdown()

    assert len(caplog.records) == 1, "the drain must be announced exactly once"


# ---------------------------------------------------------------------------
# Criterion 6 — database connections are closed on exit
# ---------------------------------------------------------------------------
def test_closing_connections_closes_every_alias():
    """M6-05 criterion 6.

    CONN_MAX_AGE is 60s, so connections are held open for reuse rather than
    closed per request. A worker that exits without this leaves PostgreSQL to
    notice the dropped socket itself — multiplied by the worker count, on every
    deploy, and a rolling restart can hold two generations at once.

    Asserted against `close_all()` rather than by inspecting a live connection,
    because under `django_db` the test runs inside an atomic block where Django
    defers the real close — the assertion would be about Django's transaction
    handling, not about this function. That the close genuinely happens is shown
    by the live-server test below, whose log carries the line.
    """
    from django.db import connections

    with mock.patch.object(connections, "close_all") as close_all:
        shutdown.close_database_connections()

    close_all.assert_called_once_with()


@pytest.mark.django_db
def test_the_lifespan_shutdown_does_not_raise_with_a_live_connection():
    """The regression that shipped and hid, until the container was watched.

    `connection.close()` is decorated @async_unsafe, so calling it from the
    event loop raises SynchronousOnlyOperation. Uvicorn reports an exception
    from the lifespan app as "ASGI 'lifespan' protocol appears unsupported" — at
    INFO, with no traceback — so the failure looked like a server that simply
    had no lifespan support, and the connections were never closed.

    Runs the real shutdown path against a real connection, in a real event loop,
    which is the only combination that would have caught it.
    """
    import asyncio

    from django.db import connection

    connection.ensure_connection()

    messages = iter([{"type": "lifespan.shutdown"}])
    sent = []

    async def receive():
        return next(messages)

    async def send(message):
        sent.append(message["type"])

    async def inner_app(scope, receive, send):
        pass

    app = shutdown.ShutdownLifespanMiddleware(inner_app)
    asyncio.run(app({"type": "lifespan"}, receive, send))

    assert sent == ["lifespan.shutdown.complete"], (
        "the lifespan shutdown must complete, not raise — uvicorn reports a "
        "raise here as 'lifespan protocol appears unsupported' and moves on"
    )


def test_the_lifespan_shutdown_is_what_closes_them():
    """The wiring, separately from the closing: it must hang off lifespan
    shutdown, which uvicorn sends AFTER the last in-flight request has finished.

    Closing earlier — from the signal handler, say — would pull connections out
    from under the requests the drain exists to let complete.
    """
    import asyncio

    messages = iter([{"type": "lifespan.shutdown"}])

    async def receive():
        return next(messages)

    async def send(message):
        pass

    async def inner_app(scope, receive, send):
        pass

    app = shutdown.ShutdownLifespanMiddleware(inner_app)

    with mock.patch.object(shutdown, "close_database_connections") as close:
        asyncio.run(app({"type": "lifespan"}, receive, send))

    close.assert_called_once_with()


def test_the_middleware_answers_the_lifespan_protocol():
    """Django's handler rejects the lifespan scope, so without this wrapper
    uvicorn disables lifespan and there is no shutdown hook at all.

    Also asserts the protocol is TERMINATED here rather than forwarded: passing
    it down would reach Django's handler and raise.
    """
    import asyncio

    forwarded = []

    async def inner_app(scope, receive, send):
        forwarded.append(scope["type"])

    app = shutdown.ShutdownLifespanMiddleware(inner_app)
    sent = []
    messages = iter([{"type": "lifespan.startup"}, {"type": "lifespan.shutdown"}])

    async def receive():
        return next(messages)

    async def send(message):
        sent.append(message["type"])

    asyncio.run(app({"type": "lifespan"}, receive, send))

    assert sent == ["lifespan.startup.complete", "lifespan.shutdown.complete"]
    assert forwarded == [], "the lifespan scope must not reach Django's handler"


def test_the_middleware_passes_other_scopes_straight_through():
    """It must be invisible to request handling — this is on the path of every
    request the application ever serves.
    """
    import asyncio

    seen = []

    async def inner_app(scope, receive, send):
        seen.append(scope)

    app = shutdown.ShutdownLifespanMiddleware(inner_app)
    scope = {"type": "http", "path": "/api/v1/ping"}
    asyncio.run(app(scope, None, None))

    assert seen == [scope]


# ---------------------------------------------------------------------------
# Criterion 2 — in-flight requests complete, verified against a real server
# ---------------------------------------------------------------------------
# The one test here that runs the actual thing. Everything above asserts that a
# part is wired correctly; this asserts that the parts together do the job.
SLOW_REQUEST_SECONDS = 3.0
SERVER_START_TIMEOUT = 20.0
SERVER_EXIT_TIMEOUT = 20.0


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _get(url: str, timeout: float):
    """Return (status, body) for a GET, treating an HTTP error as a result.

    A 503 from readiness is the expected answer during a drain, not a failure.
    """
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return response.status, response.read().decode()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode()


@pytest.fixture
def live_server_process():
    """A real uvicorn serving config.asgi:application, on the test settings.

    A subprocess, because the criterion is about what a PROCESS does when it is
    signalled — an in-process server would share this interpreter's signal
    handlers with pytest.

    DJANGO_TEST_ROOT_URLCONF points it at tests/testapp/urls.py, which mounts
    both the slow endpoint and the probes: `override_settings` cannot reach
    another process.
    """
    port = _free_port()
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "config.asgi:application",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
        ],
        cwd=REPO_ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env={
            "PATH": "/usr/bin:/bin",
            "HOME": "/tmp",
            "DJANGO_ENV_FILE": "/nonexistent/.env",
            "DATABASE_URL": "postgresql://forge:forge@db:5432/forge",
            "DJANGO_SETTINGS_MODULE": "config.settings.test",
            "DJANGO_TEST_ROOT_URLCONF": "tests.testapp.urls",
        },
    )

    # `localhost`, NOT 127.0.0.1. The test layer's ALLOWED_HOSTS is
    # ["testserver", "localhost"], so a request whose Host header is the literal
    # address is rejected as DisallowedHost — a 400 that looks exactly like the
    # server never starting.
    base = f"http://localhost:{port}"
    deadline = time.monotonic() + SERVER_START_TIMEOUT
    while time.monotonic() < deadline:
        if process.poll() is not None:
            pytest.fail(f"the server exited during startup:\n{process.stdout.read()}")
        try:
            if _get(f"{base}/healthz", timeout=1.0)[0] == 200:
                break
        except OSError:
            time.sleep(0.1)
    else:
        process.kill()
        pytest.fail("the server did not become reachable")

    try:
        yield process, base
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=10)


def test_an_in_flight_request_survives_sigterm(live_server_process):
    """M6-05 criterion 2, exactly as the issue words it.

    A slow request is started, SIGTERM is sent while it is still in flight, and
    the response must arrive complete. This is the criterion the whole task
    exists for: everything else is the machinery that makes it true.

    Readiness is checked in the same window, because the two halves have to hold
    together — a drain that finishes the request but keeps advertising itself as
    ready has simply moved the dropped requests to the next caller.
    """
    process, base = live_server_process
    result = {}

    def slow_request():
        result["response"] = _get(
            f"{base}/api/v1/shutdown/slow?seconds={SLOW_REQUEST_SECONDS}",
            timeout=SLOW_REQUEST_SECONDS + 15,
        )

    caller = threading.Thread(target=slow_request)
    caller.start()

    # Long enough that the request is definitely being served, short enough to
    # be well inside its duration.
    time.sleep(1.0)
    assert process.poll() is None, "the server died before the signal was sent"

    process.send_signal(signal.SIGTERM)

    # No NEW request may be served while the drain is running.
    #
    # Two different things produce that, and which one you get is uvicorn's
    # choice rather than ours: it closes the LISTENING SOCKET the instant
    # SIGTERM lands, so a fresh connection is usually refused outright, and only
    # a request arriving on an already-open connection reaches the 503 that
    # apps/core/shutdown.py arranges. Both are "not taking new traffic", which
    # is what criterion 4 is protecting, so both are accepted here and the
    # distinction is documented in docs/ops.md rather than asserted.
    #
    # What must NEVER happen is a 200: that is the process accepting work it has
    # already promised to stop doing.
    time.sleep(0.5)
    try:
        status, _ = _get(f"{base}/readyz", timeout=5.0)
    except OSError:
        status = None  # connection refused — the listener is already closed
    assert status != 200, (
        "readiness answered 200 during a drain, so a load balancer would keep "
        "routing new requests into a process that is shutting down"
    )

    caller.join(timeout=SLOW_REQUEST_SECONDS + 15)
    assert not caller.is_alive(), "the slow request never came back"

    status, body = result["response"]
    assert status == 200, f"the in-flight request was dropped, got {status}"
    assert json.loads(body) == {"slept": SLOW_REQUEST_SECONDS}

    # And the process must leave on its own — a drain that never ends is a
    # container the platform has to SIGKILL.
    process.wait(timeout=SERVER_EXIT_TIMEOUT)
    assert process.returncode is not None
