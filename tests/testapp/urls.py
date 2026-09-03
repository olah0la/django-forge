"""A URLconf that exists only so test-only routers can be reached over HTTP.

The schema round trip (M5-03) has to go through real request parsing: it is the
only way `PatchDict` can be shown to hand the view exactly the keys the client
sent, and the only way a response body can be inspected for a field that must
not be there. Pagination (M5-04) is the same — the ceiling is enforced by query
parameter validation, which only happens on a real request. Calling the view
functions directly would prove neither. Logging (M6-04) is the same again, and
more so: the claim being tested is that a secret sent over the wire never
reaches a log line, and only a real request has a wire.

Activated per test with `override_settings(ROOT_URLCONF="tests.testapp.urls")`,
so the project's own URLconf is untouched and nothing here is reachable in a
running application.
"""

from django.conf import settings
from django.urls import path

from apps.core.health import liveness, readiness
from config.api import V1_PREFIX, build_api
from tests.testapp.api import logging_router, shutdown_router, users_router
from tests.testapp.api import router as things_router

api = build_api()

# A namespace of its own. `.urls` registers the namespace and Ninja rejects a
# duplicate, so sharing V1_NAMESPACE with the real instance would raise
# ConfigError the moment this module is imported — see config/api.py.
api.urls_namespace = "api-v1-testapp"

# Before `.urls` is read below: Ninja freezes the router list at that point.
api.add_router("users", users_router)
api.add_router("things", things_router)
api.add_router("logging", logging_router)
api.add_router("shutdown", shutdown_router)

# The probes are mounted here too, at the SAME paths config/urls.py uses.
#
# The graceful-shutdown test (M6-05) runs a real server against this URLconf and
# has to watch readiness flip to 503 while a slow request from the router above
# is still in flight. Both have to be reachable from one URLconf for that to be
# a single observation rather than two hopeful ones.
#
# Derived from settings, not written out, so this cannot drift from the real
# URLconf and quietly test a path nothing serves.
LIVENESS_PATH, READINESS_PATH = settings.HEALTH_CHECK_PATHS

urlpatterns = [
    path(LIVENESS_PATH.lstrip("/"), liveness, name="liveness"),
    path(READINESS_PATH.lstrip("/"), readiness, name="readiness"),
    path(f"api/{V1_PREFIX}/", api.urls),
]
