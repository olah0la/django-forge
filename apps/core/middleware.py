"""Request correlation and the request log (M6-04).

One middleware doing two halves of one job: give the request an identifier that
every log line it produces will carry, and emit the single line that says what
the request was and how it went.

See docs/logging.md for the format, the header contract, and what is
deliberately never written to a log.
"""

import logging
import re
import time
from collections.abc import Awaitable, Callable
from typing import cast

from asgiref.sync import iscoroutinefunction, markcoroutinefunction
from django.conf import settings
from django.http import HttpRequest, HttpResponse

from apps.core.logging import request_id_var
from apps.core.uuid7 import uuid7

# Django hands middleware a plain callable under WSGI and a coroutine function
# under ASGI. This class handles both, and which one it got is decided once, at
# construction — see `async_capable` below.
GetResponse = (
    Callable[[HttpRequest], HttpResponse] | Callable[[HttpRequest], Awaitable[HttpResponse]]
)

# Named for what it logs, not for the module it lives in. `apps` is configured
# as a logger in config/settings/base.py, so this inherits the project's level
# and needs no handler of its own.
logger = logging.getLogger("apps.request")

# What an inbound identifier is allowed to look like.
#
# THE ANCHOR IS `\Z`, NOT `$`. In Python `$` also matches immediately before a
# trailing newline, so `"abc\n"` satisfies `^[\w-]+$` — and an identifier
# carrying a newline is written verbatim into the log stream, where it becomes
# a second line that a parser reads as a separate record. That is log injection
# in one character, from a header any client can set.
#
# The length cap is the other half: the value is held for the life of the
# request and copied into every line it produces, so an unbounded header is an
# unbounded cost per request, chosen by the caller.
VALID_REQUEST_ID = re.compile(r"\A[A-Za-z0-9._-]{1,128}\Z")


class RequestIDMiddleware:
    """Correlate a request's log lines, and log the request itself.

    Belongs FIRST in MIDDLEWARE. Middleware wraps in list order, so the first
    entry is the outermost: the identifier is set before anything else runs and
    the response header is attached after everything else has finished, which
    means a line logged by any other middleware — including one that rejects
    the request outright — is still correlated.
    """

    # Declared for BOTH, and adapted at construction. Django hands this class a
    # coroutine `get_response` under ASGI and a plain callable under WSGI; a
    # sync-only middleware in an ASGI stack is not an error, it just makes
    # Django push every single request through a threadpool hop to get past it.
    # ASGI is the served interface here (M3-04), so that hop would be paid on
    # every request for the life of the project.
    async_capable = True
    sync_capable = True

    def __init__(self, get_response: GetResponse) -> None:
        self.get_response = get_response
        # Decided once, here, rather than per request. `markcoroutinefunction`
        # is what tells Django this instance is awaitable, so it stops wrapping
        # the call in a threadpool.
        self.async_mode = iscoroutinefunction(get_response)
        if self.async_mode:
            markcoroutinefunction(self)

    def __call__(self, request: HttpRequest) -> HttpResponse | Awaitable[HttpResponse]:
        if self.async_mode:
            return self.__acall__(request)

        # The casts carry no runtime cost and no runtime risk: `async_mode`
        # above is exactly the question of which of the two shapes Django
        # handed us, and each branch has already answered it.
        get_response = cast(Callable[[HttpRequest], HttpResponse], self.get_response)

        request_id = self.begin(request)
        token = request_id_var.set(request_id)
        started = time.perf_counter()
        try:
            response = get_response(request)
            self.finish(request, response, request_id, started)
            return response
        finally:
            # In a `finally` because the context must be left as it was found,
            # even when the response never arrives. The thread that ran this
            # goes on to serve other work — Django's threadpool reuses its
            # threads — and a stale identifier is not a missing one, it is a
            # WRONG one, quietly filing later lines under an earlier request.
            request_id_var.reset(token)

    async def __acall__(self, request: HttpRequest) -> HttpResponse:
        get_response = cast(Callable[[HttpRequest], Awaitable[HttpResponse]], self.get_response)

        request_id = self.begin(request)
        token = request_id_var.set(request_id)
        started = time.perf_counter()
        try:
            response = await get_response(request)
            self.finish(request, response, request_id, started)
            return response
        finally:
            request_id_var.reset(token)

    # ----------------------------------------------------------------------

    def begin(self, request: HttpRequest) -> str:
        """Settle this request's identifier and put it where both readers look.

        The context variable is how a log call anywhere in the stack finds it,
        and it covers everything that runs inside the middleware chain.

        The attribute on the request covers what does not. Django logs 4xx and
        5xx responses AFTER the chain has returned, from
        `BaseHandler.get_response` and its async twin, where the context has
        already been reset — those records carry the request, so the identifier
        travels on it instead. RequestIDFilter.from_the_record has the
        measurement that found this.
        """
        request_id = self.resolve_request_id(request)
        request.request_id = request_id  # type: ignore[attr-defined]
        return request_id

    def resolve_request_id(self, request: HttpRequest) -> str:
        """Take the caller's identifier if it is usable, otherwise mint one.

        Accepting an inbound value is what lets one request be followed across
        service boundaries: the caller passes on the identifier it was given,
        and the two services' logs join on it.

        A rejected value is REPLACED, not refused. The alternative is a 400 for
        a malformed header, which turns a logging convenience into a reason the
        request failed.
        """
        header = settings.REQUEST_ID_HEADER
        inbound = request.META.get("HTTP_" + header.upper().replace("-", "_"), "")
        if VALID_REQUEST_ID.match(inbound):
            return inbound

        # UUIDv7, reusing apps/core/uuid7.py — the same identifier the project
        # uses for primary keys. Its leading bits are a millisecond timestamp,
        # so generated ids sort chronologically, which is a small gift when
        # they are what you are sorting a log window by.
        return str(uuid7())

    def finish(
        self,
        request: HttpRequest,
        response: HttpResponse,
        request_id: str,
        started: float,
    ) -> None:
        """Return the identifier to the caller, and log the request."""
        # Set before the exclusion check: an endpoint that is too noisy to log
        # is still an endpoint whose response should carry the identifier, so a
        # caller reporting a failed health probe has something to quote.
        response[settings.REQUEST_ID_HEADER] = request_id

        if request.path in settings.REQUEST_LOG_EXCLUDED_PATHS:
            return

        # `request.path`, NEVER `request.get_full_path()`. The query string is
        # where password-reset codes, invite tokens and signed URLs live, and a
        # scrubber that tries to clean it is a scrubber that eventually meets a
        # parameter name nobody added to the list. Not logging it cannot miss.
        logger.info(
            "%s %s %s",
            request.method,
            request.path,
            response.status_code,
            extra={
                "method": request.method,
                "path": request.path,
                "status": response.status_code,
                # perf_counter, not time.time: a monotonic clock, so an NTP
                # correction mid-request cannot produce a negative duration.
                "duration_ms": round((time.perf_counter() - started) * 1000, 2),
            },
        )
