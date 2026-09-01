"""The versioned API instance.

One `NinjaAPI` object, mounted by `config/urls.py` at `/api/v1/`. Every router
in the project attaches to it — from M5-02 each app contributes one — so this
module is the single place that answers "what is the API, and where does it
live?".

It sits in `config/` rather than in an app because it is project wiring, the
same category as the root URLconf and the ASGI entrypoint. Endpoints belong to
apps; the instance that mounts them does not. See docs/layout.md.

**Read docs/api.md before changing the URL prefix or the versioning scheme.**
That is the one decision here that cannot be revised once a client exists.
"""

from django.conf import settings
from ninja import NinjaAPI

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


def build_api() -> NinjaAPI:
    """Construct the v1 API instance from settings.

    A function rather than a bare module-level constructor call so the
    configuration can be exercised: `docs_url` depends on a setting, and a
    constructor that runs at import time freezes that value for the life of the
    process, where `override_settings` cannot reach it.

    Do not access `.urls` on an instance built here for inspection. Ninja
    registers `urls_namespace` at that point and rejects a duplicate, so a
    second instance sharing V1_NAMESPACE would raise ConfigError.
    """
    return NinjaAPI(
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


api = build_api()


# ---------------------------------------------------------------------------
# TODO(M5-02): this endpoint moves to apps/core/api.py as a router.
# ---------------------------------------------------------------------------
# M5-02 establishes the rule that the central instance mounts routers and holds
# NO endpoint definitions of its own — otherwise this module grows without
# bound and becomes a permanent merge-conflict site, since every feature branch
# would edit it. This one lives here only until that pattern exists, so that
# M5-01 can demonstrate a response through the versioned path.
@api.get("/ping", summary="Liveness of the API layer", tags=["meta"])
def ping(request) -> dict:
    """Confirm the API is mounted and answering.

    NOT a health check. M6-01 owns readiness and liveness endpoints, and those
    have to answer for the database and any other dependency. This answers for
    exactly one thing: that routing reached django-ninja.
    """
    return {"pong": True, "version": api.version}
