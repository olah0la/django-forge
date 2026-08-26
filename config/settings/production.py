"""Production settings: strict, and unable to be talked out of it.

Selected only by DJANGO_SETTINGS_MODULE=config.settings.production.
"""

import os

from django.core.exceptions import ImproperlyConfigured

from .base import *  # noqa: F403

# --------------------------------------------------------------------------
# DEBUG is off, and cannot be turned on
# --------------------------------------------------------------------------
# A literal, not a value read from anywhere. Debug pages disclose file paths,
# settings, and query fragments to anyone who triggers an error.
DEBUG = False

# Refuse to start if someone tried to enable it, rather than ignoring them in
# silence. A setting that is quietly discarded gets reported as "broken" and
# worked around; one that explains itself gets fixed.
_requested_debug = os.environ.get("DJANGO_DEBUG", "0").strip().lower()
if _requested_debug not in ("", "0", "false", "no", "off"):
    raise ImproperlyConfigured(
        f"DJANGO_DEBUG={_requested_debug!r} was set, but DEBUG cannot be enabled "
        "in the production settings layer. If you need debug output, run with "
        "DJANGO_SETTINGS_MODULE=config.settings.development instead."
    )

# TODO(M3-05): source from the environment, and fail when unset. A permissive
# default here would defeat Django's Host-header protection entirely.
ALLOWED_HOSTS = [
    h.strip() for h in os.environ.get("DJANGO_ALLOWED_HOSTS", "").split(",") if h.strip()
]

# TODO(M3-05): the remaining deploy hardening (HSTS, secure cookies, SSL
# redirect) lands there, verified by `manage.py check --deploy`.
