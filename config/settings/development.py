"""Development settings: convenience over strictness.

Nothing in this module should ever be reachable in production. It is selected
only by DJANGO_SETTINGS_MODULE=config.settings.development.
"""

import os

from .base import *  # noqa: F403

# Verbose error pages with tracebacks and local variables. Never true in
# production — those pages disclose file paths, settings, and query fragments.
DEBUG = os.environ.get("DJANGO_DEBUG", "1") == "1"

# Permissive on purpose, and safe only because this layer is never deployed.
# "app" is the Compose service name, which is how the container reaches itself;
# runserver binds 0.0.0.0 so the published port works from the host.
ALLOWED_HOSTS = ["localhost", "127.0.0.1", "0.0.0.0", "app", "[::1]"]

# Email goes to the console rather than anywhere real, so a stray send during
# development cannot reach an actual person.
EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"
