"""The core app's router — meta endpoints for the API itself.

**The convention every app follows.** One router per app, named `router`, in
`apps/<name>/api.py`. `config/api.py` mounts it and nothing else, which is what
keeps that file from becoming a permanent merge-conflict site: adding an
endpoint here touches this file only.

An app that outgrows a single module turns `api.py` into a package —
`apps/<name>/api/` with an `__init__.py` that re-exports `router` — so the
import path `apps.<name>.api.router` never changes and `config/api.py` does not
have to know which shape an app is in.

**This module must never import `config.api`.** Endpoints are attached to the
`Router`, never to the `NinjaAPI` instance, and the dependency runs one way:
config imports apps. Reaching back the other way creates an import cycle that
surfaces as a confusing failure at startup rather than where it was written.

Routers are `RouterPaginated` (M5-04), so a list endpoint is paginated whether
or not its author remembered to ask.

`core` is the one router mounted at the API root rather than behind a prefix —
`/api/v1/ping`, not `/api/v1/core/ping`. Meta endpoints answer for the API as a
whole and are not a resource collection. Feature apps always take a prefix; see
docs/api.md, which also explains why that exception is not extended.
"""

from django.conf import settings
from django.http import HttpRequest
from ninja.pagination import RouterPaginated

# `RouterPaginated`, not `Router` — the convention for every app router here.
# It paginates any operation whose `response=` is a collection, so a list
# endpoint cannot be shipped unpaginated by forgetting a decorator. Endpoints
# that return a single object, like `ping` below, are untouched.
#
# Tagged once, here, rather than on each endpoint. Every operation on the router
# inherits it, so the OpenAPI document groups them together and no endpoint can
# be added untagged by forgetting a decorator argument.
router = RouterPaginated(tags=["meta"])


@router.get("/ping", summary="Liveness of the API layer")
def ping(request: HttpRequest) -> dict:
    """Confirm the API is mounted and answering.

    NOT a health check. `/healthz` and `/readyz` (apps/core/health.py) are the
    health endpoints, they live outside /api/v1/ because a probe URL is an
    infrastructure contract rather than an API one, and readiness answers for
    the database. This answers for exactly one thing: that routing reached
    django-ninja. Do not point a probe at it — see docs/ops.md.

    The version is read from settings rather than from the API instance, since
    this module does not import `config.api`. It is the OpenAPI *document*
    version, not the `v1` in the URL — see docs/api.md.
    """
    return {"pong": True, "version": settings.API_VERSION}
