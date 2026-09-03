"""ASGI entrypoint — the interface both profiles are served through.

ASGI, not WSGI, on purpose. WSGI handles one request per worker at a time;
ASGI additionally supports asynchronous views and long-lived connections.
Django Ninja supports async endpoints, so choosing WSGI would foreclose that
and reversing it later would touch the server, the container, and every
endpoint written in between. See docs/adr and docs/layout.md.

Nothing forces a view to be async. This keeps the option open; individual
views opt in.

**The constraint worth knowing before you write one:** Django's ORM is
synchronous. Calling it directly from an `async def` view raises
`SynchronousOnlyOperation`. Use the async ORM methods (`aget`, `acreate`,
`async for`) or wrap synchronous work in `sync_to_async`. Async Django is not
"Django but faster" — see docs/layout.md.
"""

from django.core.asgi import get_asgi_application

from config import require_settings_module

require_settings_module()

# The Django half of the stack. Named separately from `application` because
# the two have different types once the wrappers below are applied, and
# reassigning one name through them is what mypy objects to.
django_application = get_asgi_application()

# --------------------------------------------------------------------------
# Static files in development
# --------------------------------------------------------------------------
# `runserver` used to serve static files automatically in DEBUG. uvicorn does
# not, so without this the admin renders unstyled and looks broken.
#
# Applied only under DEBUG, so production is untouched: M6-03 owns the real
# static-serving strategy, and serving them from the application process is
# not it.
#
# Imported here rather than at module top so settings are configured first.
from django.conf import settings  # noqa: E402

if settings.DEBUG:
    from django.contrib.staticfiles.handlers import ASGIStaticFilesHandler  # noqa: E402

    django_application = ASGIStaticFilesHandler(django_application)

# --------------------------------------------------------------------------
# Graceful shutdown (M6-05)
# --------------------------------------------------------------------------
# OUTERMOST, and that is the whole reason it is applied last. Django's handler
# serves `http` and rejects every other scope type, so without a wrapper uvicorn
# finds lifespan unsupported and disables it — the process then has no startup
# or shutdown hook at all.
#
# This wrapper answers the lifespan protocol itself and passes every other scope
# straight through, so request handling is untouched. What it buys: readiness
# starts reporting 503 the moment SIGTERM lands, and database connections are
# closed once the last in-flight request has finished.
#
# Applied in BOTH profiles on purpose. A drain that only exists in production is
# a drain nobody exercises until a deploy goes wrong. See apps/core/shutdown.py
# and docs/ops.md.
from apps.core.shutdown import ShutdownLifespanMiddleware  # noqa: E402

application = ShutdownLifespanMiddleware(django_application)
