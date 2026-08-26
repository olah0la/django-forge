"""Production settings: strict, and unable to be talked out of it.

Selected only by DJANGO_SETTINGS_MODULE=config.settings.production.
"""

from django.core.exceptions import ImproperlyConfigured

from .base import *  # noqa: F403
from .base import env, require

# --------------------------------------------------------------------------
# DEBUG is off, and cannot be turned on
# --------------------------------------------------------------------------
# A literal, not a value read from anywhere. Debug pages disclose file paths,
# settings, and query fragments to anyone who triggers an error.
DEBUG = False

# Refuse to start if someone tried to enable it, rather than ignoring them in
# silence. A setting that is quietly discarded gets reported as "broken" and
# worked around; one that explains itself gets fixed.
if env.bool("DJANGO_DEBUG", default=False):
    raise ImproperlyConfigured(
        "DJANGO_DEBUG was set, but DEBUG cannot be enabled in the production "
        "settings layer. If you need debug output, run with "
        "DJANGO_SETTINGS_MODULE=config.settings.development instead."
    )

# --------------------------------------------------------------------------
# Required — no defaults, on purpose
# --------------------------------------------------------------------------
# No `default=`, so django-environ raises ImproperlyConfigured naming the
# variable when it is missing. Refusing to start beats starting insecurely:
# a fallback key would be identical in every project forged from this template,
# which makes every session cookie in all of them forgeable.
SECRET_KEY = require("DJANGO_SECRET_KEY")

# Likewise required. An empty or wildcard default would defeat Django's
# Host-header protection entirely.
# require() first, so an empty value fails loudly instead of yielding [].
ALLOWED_HOSTS = [h.strip() for h in require("DJANGO_ALLOWED_HOSTS").split(",") if h.strip()]

# TODO(M3-05): the remaining deploy hardening (HSTS, secure cookies, SSL
# redirect) lands there, verified by `manage.py check --deploy`.
