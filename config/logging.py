"""Logging helpers referenced by the LOGGING dict in config/settings/base.py.

Today this holds one thing: the filter that keeps health-check probes out of the
request logs (M6-01). M6-04 grows it into the structured-logging configuration —
JSON in production, a correlation identifier on every request-scoped line — and
this filter stays, because a probe every few seconds drowns those logs just as
effectively.

Named `logging` inside the `config` package on purpose, and it is safe: Python 3
resolves `import logging` below to the standard library, not to this module.
"""

import logging

from django.conf import settings

# TWO loggers emit a line per probe, and only one of them is obvious.
#
# `uvicorn.access` logs every request:
#
#     access_logger.info(
#         '%s - "%s %s HTTP/%s" %d',
#         client_addr, method, path_with_query, http_version, status_code,
#     )
#
# so the path is args[2], with the query string still attached. Verified against
# uvicorn 0.52 (protocols/http/httptools_impl.py and h11_impl.py).
#
# `django.request` logs every 4xx and 5xx — which, during a database outage, is
# EVERY readiness probe, at ERROR, from every replica. That one was found by
# running the server and reading the log, not by reasoning about it.
#
# The uvicorn shape is coupling to another project's log call, and it is
# accepted rather than avoided because uvicorn formats the line before anything
# of ours sees it: a filter on the record is the only place the path is still a
# separate value. The failure mode is bounded by _record_path() returning None.
_ACCESS_LOG_ARG_COUNT = 5
_ACCESS_LOG_PATH_INDEX = 2


def _record_path(record: logging.LogRecord) -> str | None:
    """The request path this record describes, or None if it does not describe one.

    None means "do not know", never "empty path". Everything downstream treats
    it as a record to KEEP.
    """
    # django.request attaches the request itself through `extra` (see
    # django.utils.log.log_response). A documented attribute, so this half is
    # not coupled to a format string.
    path = getattr(getattr(record, "request", None), "path", None)
    if isinstance(path, str):
        return path

    args = record.args
    if not isinstance(args, tuple) or len(args) != _ACCESS_LOG_ARG_COUNT:
        return None

    path = args[_ACCESS_LOG_PATH_INDEX]
    if not isinstance(path, str):
        return None

    # get_path_with_query_string() appends "?..." when there is a query string.
    return path.split("?", 1)[0]


class SuppressHealthCheckAccessLogs(logging.Filter):
    """Drop request-log lines for the liveness and readiness endpoints.

    M6-01 acceptance criterion: both endpoints are excluded from request logging.
    They are polled every few seconds for the entire life of every container, so
    without this they are the overwhelming majority of the log and the real
    traffic is unreadable — and, once logs are shipped somewhere paid for by
    volume, expensive.

    This does NOT silence the probes themselves. `apps/core/health.py` logs a
    warning, with the underlying exception, every time readiness fails. That is
    the line an operator needs, and it is the reason the response body can stay
    free of detail.

    **Fail open, twice over.** A record carrying an exception is always kept, and
    so is anything whose path cannot be positively identified. A filter that
    silently swallows real request logs is a far worse outcome than one that
    occasionally logs a probe — and the coupling to uvicorn's log call above is
    exactly the kind of thing that breaks quietly on an upgrade.

    The paths are read from settings at filter time rather than at import time,
    so `override_settings` reaches them and there is still only one place the
    paths are written down.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        # Never swallow a traceback. django.request logs an unhandled exception
        # at the same level and through the same logger as an ordinary 5xx, so
        # without this line a genuine crash INSIDE a probe view — the one moment
        # the traceback is indispensable — would be dropped along with the noise.
        if record.exc_info:
            return True

        path = _record_path(record)
        if path is None:
            return True
        return path not in settings.HEALTH_CHECK_PATHS
