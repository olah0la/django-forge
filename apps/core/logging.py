"""Structured logging: JSON for machines, prose for people (M6-04).

Two formats, one configuration. `config/settings/base.py` builds the `LOGGING`
dictionary from the pieces here; the layer that is running chooses which
formatter the single handler uses.

**WHY THIS MODULE MAY NOT IMPORT MODELS.** Django calls `configure_logging()`
*before* `apps.populate()`, so `logging.config.dictConfig` imports this module
while the app registry is still empty. Anything here that reaches a model
raises `AppRegistryNotReady` at startup, in a traceback that points at the
logging configuration rather than at the import. Standard library and
`django.conf.settings` only.

Nothing writes to a file. Container filesystems are ephemeral and a log file
inside one is lost with the container that wrote it, so everything goes to
stdout and the platform's collector does the rest.
"""

import json
import logging
from collections.abc import Mapping
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any

# --------------------------------------------------------------------------
# The correlation identifier
# --------------------------------------------------------------------------
# A ContextVar rather than thread-local storage, because ASGI is the served
# interface (M3-04). One worker process handles many requests concurrently in
# ONE thread, so thread-local state would hand every concurrent request the
# same identifier — the exact interleaving this exists to undo.
#
# contextvars are per-task under asyncio and are COPIED into the threadpool
# Django runs sync views in, so a value set by the middleware is visible to
# every log call made while handling that request, sync or async, however deep.
request_id_var: ContextVar[str] = ContextVar("request_id", default="")

# What a log line carries when it was not produced while handling a request —
# startup, a management command, a background thread. A literal placeholder
# rather than an empty string keeps the column aligned in the console format
# and keeps the JSON key present, so a query can filter on it.
NO_REQUEST_ID = "-"


class RequestIDFilter(logging.Filter):
    """Attach the current request's identifier to every record.

    A *filter* rather than a formatter, and installed on the HANDLER rather
    than on a logger. That placement is what makes correlation free: a line
    logged from anywhere — a view, a model method, a third-party library that
    never heard of this project — passes through the one handler and gains the
    identifier on the way. No caller has to remember to pass it.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_var.get() or self.from_the_record(record) or NO_REQUEST_ID
        # Filters double as record mutators. Always keep the record.
        return True

    @staticmethod
    def from_the_record(record: logging.LogRecord) -> str:
        """Recover the identifier from a record logged outside the request.

        **The context variable is not enough, and this was measured rather than
        predicted.** Django logs 4xx and 5xx responses from
        `BaseHandler.get_response_async`, which runs AFTER `_middleware_chain`
        has returned — outside every middleware, including the one that sets
        the identifier, and under ASGI in a different thread. Before this
        fallback existed, a 404 produced a correlated request line from this
        project and an UNCORRELATED `Not Found: /nope` from Django, one
        millisecond apart.

        (A 500 was already correlated, which is what made the gap easy to miss:
        an exception is logged by `response_for_exception` INSIDE the chain. A
        plain 4xx — a 404, a 401, one of Ninja's 422s — is not.)

        Those records carry the request itself, and the middleware puts the
        identifier on it. Only the attribute is read; the object is never
        serialised, because its repr contains the query string — see
        JSONFormatter.
        """
        request_id = getattr(getattr(record, "request", None), "request_id", "")
        return request_id if isinstance(request_id, str) else ""


# --------------------------------------------------------------------------
# Redaction
# --------------------------------------------------------------------------
# Substrings, not exact names, so `user_password`, `X-Api-Key` and
# `refresh_token` all match without enumerating every spelling.
SENSITIVE_KEY_MARKERS = frozenset(
    {
        "auth",
        "cookie",
        "credential",
        "csrf",
        "database_url",
        "key",
        "passwd",
        "password",
        "secret",
        "session",
        "signature",
        "token",
    }
)

REDACTED = "[redacted]"


def is_sensitive(key: str) -> bool:
    """Whether a mapping key looks like it names a secret."""
    lowered = key.lower()
    return any(marker in lowered for marker in SENSITIVE_KEY_MARKERS)


def redact_mapping(data: Mapping[str, Any]) -> dict[str, Any]:
    """Return `data` with the values of sensitive-looking keys masked.

    For the case where a dictionary genuinely has to be logged. It is NOT this
    project's protection against leaking secrets — that is structural, and
    docs/logging.md lists the four controls that do the real work. A key-name
    heuristic is a last line of defence, and it cannot see a secret stored
    under an innocent name.
    """
    return {key: REDACTED if is_sensitive(key) else value for key, value in data.items()}


# --------------------------------------------------------------------------
# Formatters
# --------------------------------------------------------------------------
# The extra attributes a record is allowed to publish, and the order in which
# they appear. See JSONFormatter for why this is an allow-list.
#
# `status` is ours, from the request middleware. `status_code` is Django's, set
# on the records `django.request` emits for a 4xx or 5xx — both are listed so a
# framework line and one of ours can be compared on the same field.
EXTRA_FIELDS = ("method", "path", "status", "status_code", "duration_ms")


class JSONFormatter(logging.Formatter):
    """One JSON object per line.

    **The allow-list is the point, not a simplification.** Django's
    `django.request` logger attaches the live `HttpRequest` to the records it
    emits for a 4xx or 5xx, as `record.request`. Its repr is
    `<ASGIRequest: POST '/api/v1/things?token=s3cret'>`, so a formatter that
    serialises "every non-standard attribute" — which is what the obvious
    implementation does, and what the JSON logging libraries do by default —
    writes that query string into the log. Only the fields named in
    EXTRA_FIELDS are published. `record.request` is dropped, and so is whatever
    a future dependency starts attaching that nobody has thought about yet.

    `default=str` and `ensure_ascii=False` on the dump: a formatter that raises
    does not produce a broken line, it produces NO line — and the surprising
    value that caused it is exactly the one worth having in the log.
    """

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": self.format_timestamp(record),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            # RequestIDFilter puts this here. getattr with a default anyway: a
            # record can reach a handler without passing that filter (pytest's
            # caplog attaches its own handler and does exactly that), and a
            # KeyError raised inside a formatter loses the line entirely.
            "request_id": getattr(record, "request_id", NO_REQUEST_ID),
        }

        for field in EXTRA_FIELDS:
            value = getattr(record, field, None)
            if value is not None:
                payload[field] = value

        payload["module"] = record.module
        payload["line"] = record.lineno

        # Last, because it is by far the longest value and a human scanning raw
        # lines should not have to skip a traceback to reach the fields.
        # json.dumps escapes the newlines, so a multi-line traceback is still
        # exactly ONE line of output — which is what keeps the stream parseable
        # by anything that reads it a line at a time.
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        if record.stack_info:
            payload["stack"] = self.formatStack(record.stack_info)

        return json.dumps(payload, ensure_ascii=False, default=str)

    @staticmethod
    def format_timestamp(record: logging.LogRecord) -> str:
        """ISO-8601, UTC, milliseconds, `Z` suffix.

        Not `logging`'s default `%(asctime)s`, which is LOCAL time, with a
        comma before the milliseconds and no offset at all — three separate
        reasons a log aggregator either refuses it or quietly assumes a
        timezone, and the second one only bites when the machine is not on UTC.
        """
        moment = datetime.fromtimestamp(record.created, tz=UTC)
        return moment.isoformat(timespec="milliseconds").replace("+00:00", "Z")


# The development format. `[%(request_id)s]` is the same identifier the JSON
# format publishes, so the habit of grepping for one transfers unchanged from a
# laptop to production.
#
# Short clock time and no date: on a laptop the date is today, and the width
# that saves goes to the message.
CONSOLE_FORMAT = "%(asctime)s %(levelname)-8s %(name)s [%(request_id)s] %(message)s"
CONSOLE_DATE_FORMAT = "%H:%M:%S"
