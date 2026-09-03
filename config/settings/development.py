"""Development settings: convenience over strictness.

Nothing in this module should ever be reachable in production. It is selected
only by DJANGO_SETTINGS_MODULE=config.settings.development.
"""

from django.core.management.utils import get_random_secret_key

from .base import *  # noqa: F403
from .base import build_logging, database_from_url, env

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

# Human-readable logs, because a person is reading them.
#
# THE OVERRIDE IS THE POINT. Running two formats means the one that matters is
# the one nobody looks at until it is deployed — the cost this project accepted
# knowingly. `DJANGO_LOG_FORMAT=json make up` shows production's exact output
# on a laptop, which is how a formatting bug gets found before it ships rather
# than during an incident. See docs/logging.md.
LOGGING = build_logging(env.str("DJANGO_LOG_FORMAT", default="console"))

# PostgreSQL, from DATABASE_URL. See database_from_url() for the connection
# reuse settings and the worker-count arithmetic that bounds them.
DATABASES = {"default": database_from_url()}

# `manage.py seed` is allowed here. See base.py for why this is a code-level
# layer decision rather than an environment variable.
SEED_ENABLED = True

# Interactive API documentation at /api/v1/docs. On here, off in production —
# acceptance criterion of M5-01, and the reason the docs are worth having at
# all: an endpoint you can call from the browser while writing it.
API_DOCS_ENABLED = True

# --------------------------------------------------------------------------
# Static files (M6-03)
# --------------------------------------------------------------------------
# WhiteNoise serves static in EVERY layer, so the mechanism that runs on a
# laptop is the mechanism that runs in production. These two settings are the
# only difference, and both exist so that no local `collectstatic` is required.
#
# Resolve through the staticfiles finders — each app's own static/ directory —
# rather than through STATIC_ROOT. Without it, WhiteNoise looks for a
# `staticfiles/` that a checkout does not have, logs "No directory at:", and
# serves nothing: the admin renders unstyled and looks broken.
WHITENOISE_USE_FINDERS = True

# Re-check the file on every request instead of caching the directory scan at
# startup, so an edited stylesheet appears on reload. This is the setting that
# would be actively wrong in production — it stats the filesystem per request —
# which is exactly why it is here and not in base.
WHITENOISE_AUTOREFRESH = True
