"""Development settings: convenience over strictness.

Nothing in this module should ever be reachable in production. It is selected
only by DJANGO_SETTINGS_MODULE=config.settings.development.
"""

from django.core.management.utils import get_random_secret_key

from .base import *  # noqa: F403
from .base import env

# Typed, so the literal string "False" becomes False rather than a truthy
# non-empty string. Defaults to on: this layer is never deployed.
DEBUG = env.bool("DJANGO_DEBUG", default=True)

# Generated when not supplied, so no insecure key is committed to the
# repository. The trade-off is real: a fresh key each restart invalidates
# sessions and signed cookies, so logins do not survive a reload. Set
# DJANGO_SECRET_KEY in .env to pin one — see .env.example.
SECRET_KEY = env.str("DJANGO_SECRET_KEY", default="") or get_random_secret_key()

# Permissive on purpose, and safe only because this layer is never deployed.
ALLOWED_HOSTS = env.list(
    "DJANGO_ALLOWED_HOSTS",
    default=["localhost", "127.0.0.1", "0.0.0.0", "app", "[::1]"],
)

# Email goes to the console rather than anywhere real, so a stray send during
# development cannot reach an actual person.
EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"
