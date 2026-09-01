"""A URLconf that exists only so test-only routers can be reached over HTTP.

The schema round trip (M5-03) has to go through real request parsing: it is the
only way `PatchDict` can be shown to hand the view exactly the keys the client
sent, and the only way a response body can be inspected for a field that must
not be there. Calling the view functions directly would prove neither.

Activated per test with `override_settings(ROOT_URLCONF="tests.testapp.urls")`,
so the project's own URLconf is untouched and nothing here is reachable in a
running application.
"""

from django.urls import path

from config.api import V1_PREFIX, build_api
from tests.testapp.api import users_router

api = build_api()

# A namespace of its own. `.urls` registers the namespace and Ninja rejects a
# duplicate, so sharing V1_NAMESPACE with the real instance would raise
# ConfigError the moment this module is imported — see config/api.py.
api.urls_namespace = "api-v1-testapp"

# Before `.urls` is read below: Ninja freezes the router list at that point.
api.add_router("users", users_router)

urlpatterns = [path(f"api/{V1_PREFIX}/", api.urls)]
