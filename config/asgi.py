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
# Static files are NOT wrapped here (M6-03)
# --------------------------------------------------------------------------
# This module used to wrap the application in `ASGIStaticFilesHandler` under
# DEBUG, because `runserver` serves static files automatically and uvicorn does
# not — without something, the admin renders unstyled and looks broken.
#
# That wrapper is gone. WhiteNoise is in MIDDLEWARE (config/settings/base.py)
# and serves static in EVERY layer, so the mechanism exercised on a laptop is
# the mechanism that runs in production. One fewer thing that only breaks after
# deployment.
#
# The old comment here said serving static from the application process "is not
# it", and that was true of what it was describing. `ASGIStaticFilesHandler` is
# a debug convenience: no cache headers, no compression, no content hashing,
# and it re-reads the disk on every request. WhiteNoise does all four, which is
# the entire difference between a development crutch and a static server that
# happens to live in the same process. See docs/serving.md.
