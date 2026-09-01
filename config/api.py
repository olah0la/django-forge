"""The versioned API instance, and the one place routers are mounted.

One `NinjaAPI` object, mounted by `config/urls.py` at `/api/v1/`. It **defines
no endpoints of its own**: each app owns its endpoints in `apps/<name>/api.py`,
and this module attaches those routers. That is the whole point of the split —
otherwise every feature branch edits this file and it becomes a permanent
merge-conflict site. See docs/api.md.

It sits in `config/` rather than in an app because it is project wiring, the
same category as the root URLconf and the ASGI entrypoint. Endpoints belong to
apps; the instance that mounts them does not. See docs/layout.md.

**Read docs/api.md before changing the URL prefix or the versioning scheme.**
That is the one decision here that cannot be revised once a client exists.
"""

from django.conf import settings
from ninja import NinjaAPI, Router

# The dependency runs ONE WAY: config imports apps, never the reverse. A router
# module that imports `config.api` — to reach `api` and decorate with it —
# closes an import cycle, and the error it produces at startup points at the
# import machinery rather than at the line that caused it.
#
# Importing app modules here is safe because nothing imports this file until
# `config/urls.py` does, which happens after Django's app registry is ready.
# Importing it from settings or from an AppConfig would drag app code in too
# early; don't.
from apps.core.api import router as core_router

# ---------------------------------------------------------------------------
# TWO DIFFERENT VERSION NUMBERS, and they are confused constantly
# ---------------------------------------------------------------------------
# `v1` in the URL is the CONTRACT BOUNDARY. It changes only when the API breaks
# compatibility, which is a coordinated event involving every consumer. It is
# not a release number and must not track one.
#
# `settings.API_VERSION` is the OpenAPI DOCUMENT version. It moves freely —
# 1.0.0 -> 1.1.0 on any additive change — and must never change the URL.
#
# Bumping the second is routine. Bumping the first is a project, and
# docs/api.md describes how it is done: v2 runs BESIDE v1 through a deprecation
# window, rather than replacing it.
V1_PREFIX = "v1"

# Pinned, NOT derived from API_VERSION. Ninja defaults this to "api-" + version,
# which means bumping the document version to 1.1.0 would silently rename the
# URL namespace and break every `reverse("api-1.0.0:...")` in the project. The
# namespace identifies the INSTANCE, so it follows the URL prefix instead — and
# it is what lets a future v2 instance coexist with this one.
V1_NAMESPACE = f"api-{V1_PREFIX}"

# ---------------------------------------------------------------------------
# The routers, and the only line an app adds to this file
# ---------------------------------------------------------------------------
# One entry per app: (URL prefix, router). A new app appends one line here; a
# new ENDPOINT on an existing app touches nothing here at all.
#
# The prefix is the app's resource name and carries no slashes — Ninja joins it
# to the mount point. Registration order is the order the docs page lists the
# groups in, so keep it deliberate rather than alphabetical-by-accident.
#
# `core` is the deliberate exception: it mounts at the ROOT with an empty
# prefix, because its endpoints answer for the API itself rather than for a
# collection — `/api/v1/ping`, not `/api/v1/core/ping`. Feature apps always take
# a prefix; docs/api.md explains why the exception is not extended.
ROUTERS: list[tuple[str, Router]] = [
    ("", core_router),
]


def build_api() -> NinjaAPI:
    """Construct the v1 API instance from settings, with every router mounted.

    A function rather than a bare module-level constructor call so the
    configuration can be exercised: `docs_url` depends on a setting, and a
    constructor that runs at import time freezes that value for the life of the
    process, where `override_settings` cannot reach it.

    Routers are mounted HERE rather than after the fact, so an instance built
    for inspection is the same shape as the one being served — a test asserting
    against a router-less instance would prove nothing. Mounting the same
    `Router` object on a second instance is fine (verified against ninja 1.6.3);
    what Ninja rejects is mounting it twice on the SAME instance.

    Do not access `.urls` on an instance built here for inspection. Ninja
    registers `urls_namespace` at that point and rejects a duplicate, so a
    second instance sharing V1_NAMESPACE would raise ConfigError — and reading
    `.urls` also freezes the router list, after which `add_router` raises.
    """
    api = NinjaAPI(
        title=settings.API_TITLE,
        version=settings.API_VERSION,
        description=settings.API_DESCRIPTION,
        urls_namespace=V1_NAMESPACE,
        # BOTH of these, together. None removes the route entirely, so the path
        # 404s rather than rendering an empty or error page.
        #
        # Disabling only docs_url is the mistake worth naming, because it looks
        # finished: the browsable page disappears and openapi.json keeps
        # serving. Measured on the production layer while building this — /docs
        # returned 404 and /openapi.json returned 200. The JSON is the *more*
        # useful artefact to someone enumerating an API, so hiding the page
        # alone buys nothing.
        #
        # Off in production by default; one variable turns both on together.
        # See docs/api.md.
        docs_url="/docs" if settings.API_DOCS_ENABLED else None,
        openapi_url="/openapi.json" if settings.API_DOCS_ENABLED else None,
    )

    for prefix, router in ROUTERS:
        api.add_router(prefix, router)

    return api


api = build_api()
