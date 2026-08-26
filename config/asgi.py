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

application = get_asgi_application()

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

    application = ASGIStaticFilesHandler(application)
