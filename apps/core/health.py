"""Liveness and readiness probes — the application-level answer to two questions.

An orchestrator asks a service two things, and conflating them causes outages:

    liveness   is this process healthy, or should I kill and restart it?
    readiness  should I send this process traffic right now?

A container still opening its database connections is *alive* but not *ready*.
One endpoint answering both means the platform either restarts a perfectly
healthy process because a dependency blipped, or routes traffic to one that
cannot serve it. Read docs/ops.md before changing either view.

**These are plain Django views, not django-ninja endpoints, and they are
deliberately NOT under /api/v1/.** A probe URL is an infrastructure contract
consumed by Docker and orchestrators; the API prefix is a contract boundary for
API clients. Tying them together means a future v2 either moves the probe —
breaking every deployment manifest at once — or serves it from a namespace it
does not belong to. Keeping them out of the API also means they still answer
when the NinjaAPI instance fails to build, which is exactly the moment a probe
is worth having. `config/urls.py` mounts them, and `settings.HEALTH_CHECK_PATHS`
is where the paths are written down once.

Not to be confused with `/api/v1/ping` (apps/core/api.py), which answers for
exactly one thing: that routing reached django-ninja.
"""

import logging
from collections.abc import Callable

from django.db import connections
from django.http import HttpRequest, JsonResponse
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_safe

logger = logging.getLogger(__name__)


def _database_is_reachable() -> bool:
    """One round trip to the default database.

    `SELECT 1` rather than `ensure_connection()`, because CONN_MAX_AGE is 60s
    (see `database_from_url` in config/settings/base.py) and a pooled connection
    can be open and dead at the same time — killed by a database restart or a
    network blip. Only a real round trip catches that, and it is what
    docker-entrypoint.sh already uses to wait for the database at startup.

    The cost is one trivial query every few seconds for the life of the
    container, which is the whole budget a readiness check has.

    Every exception is caught on purpose: the probe's job is to report, not to
    raise. A traceback out of here becomes a 500, which an orchestrator reads as
    "not ready" anyway but which buries the actual cause in a stack trace.
    """
    try:
        with connections["default"].cursor() as cursor:
            cursor.execute("SELECT 1")
    except Exception as exc:
        # The detail belongs HERE and not in the response body — see readiness().
        #
        # The MESSAGE, not exc_info. Measured while building this: a full chained
        # traceback for an unreachable database is ~60 lines through the ORM's
        # internals, repeated on every probe for the length of the outage — which
        # is the same log flood this issue exists to prevent, arriving at the
        # moment the log is most needed. The message alone says everything
        # actionable: `connection to server at "db" (172.18.0.2), port 5432
        # failed: Connection refused`. The stack above it is always identical.
        logger.warning("readiness: database check failed: %s", exc)
        return False
    return True


# HARD DEPENDENCIES ONLY. This is the constraint that keeps readiness safe: a
# probe that verifies a non-essential third-party service pulls the entire
# service out of rotation because of someone else's outage, and no traffic is
# served until they fix it.
#
# The test a new entry has to pass: if this dependency is down, is the service
# genuinely unable to serve ANY request? If some requests would still succeed,
# it does not belong here — degrade that feature instead.
#
# A downstream project adds its own here (a cache the service cannot run
# without, a queue it must publish to). Keep each check cheap.
READINESS_CHECKS: tuple[tuple[str, Callable[[], bool]], ...] = (
    ("database", _database_is_reachable),
)


@require_safe
@never_cache
def liveness(request: HttpRequest) -> JsonResponse:
    """Is this process healthy, or should the platform restart it?

    Touches NOTHING external — not the database, not a cache, not a queue. That
    is the entire point: a liveness probe that checks a dependency turns a
    database outage into a restart loop across every replica, which is strictly
    worse than the outage on its own.

    It answers exactly one question: is the process able to route a request
    through the middleware stack and return a response? If it cannot, restarting
    is the correct remedy — and that is the only situation in which restarting
    is the correct remedy.

    No query is issued: SessionMiddleware and AuthenticationMiddleware attach
    lazy objects and only reach the database when something touches them. A test
    pins that with assertNumQueries(0), because a middleware added later could
    silently break it.
    """
    return JsonResponse({"status": "alive"})


@require_safe
@never_cache
def readiness(request: HttpRequest) -> JsonResponse:
    """Should this process receive traffic right now?

    Runs every check in READINESS_CHECKS and returns 503 if any fails, so the
    platform stops routing to a replica that cannot serve. It does NOT mean the
    process should be restarted — see liveness() above.

    **The body carries no detail, deliberately.** Which check failed, and the
    exception behind it, are logged; the response says `{"status": "not ready"}`
    and nothing more. This endpoint is unauthenticated by necessity — a probe
    cannot hold credentials — so anything it returns is returned to whoever can
    reach the port. "The database is unreachable at db:5432" is free
    reconnaissance.

    503 rather than 500: this is a temporary inability to serve, which is what
    503 means, and load balancers treat it as such.
    """
    failed = [name for name, check in READINESS_CHECKS if not check()]

    if failed:
        # Names only, at WARNING: this line is how an operator finds out WHICH
        # dependency is down, since the response deliberately will not say.
        logger.warning("readiness: not ready (%s)", ", ".join(failed))
        return JsonResponse({"status": "not ready"}, status=503)

    return JsonResponse({"status": "ready"})
