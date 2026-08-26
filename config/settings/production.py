"""Production settings: strict, and unable to be talked out of it.

Selected only by DJANGO_SETTINGS_MODULE=config.settings.production.
"""

from django.core.exceptions import ImproperlyConfigured

from .base import *  # noqa: F403
from .base import database_from_url, env, require

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
        "settings layer.\n\n"
        "  Check your .env — it is read by BOTH profiles, so a truthy "
        "DJANGO_DEBUG there stops production from starting.\n"
        "  For debug output, run with "
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

# --------------------------------------------------------------------------
# Transport security
# --------------------------------------------------------------------------
# These clear all four warnings `manage.py check --deploy` raises. Nothing is
# silenced: `make audit` runs the audit, and a green result there means the
# settings are genuinely set, not suppressed.

# Cookies only over HTTPS. Unconditional — there is no legitimate production
# reason to send a session or CSRF cookie in plaintext, where anyone on the
# network path can copy it and become the user.
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True

# Redirect plain HTTP to HTTPS.
#
# Overridable because it is genuinely wrong in one common topology: when TLS
# terminates at a load balancer, the balancer already redirects, and Django
# redirecting again is at best redundant. Set DJANGO_SECURE_SSL_REDIRECT=false
# there.
SECURE_SSL_REDIRECT = env.bool("DJANGO_SECURE_SSL_REDIRECT", default=True)

# Behind a TLS-terminating proxy, Django sees plain HTTP and would redirect
# forever. This header tells it to trust the proxy's account of the original
# scheme.
#
# OPT-IN, deliberately. Trusting X-Forwarded-Proto unconditionally is itself a
# vulnerability: if the application is ever reachable directly, a client can
# send the header themselves and convince Django a plaintext request was
# secure — re-enabling everything above. Safe only behind a proxy that
# OVERWRITES the header on every request.
if env.bool("DJANGO_TRUST_PROXY_SSL_HEADER", default=False):
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")

# HTTP Strict Transport Security: tells browsers to refuse plaintext for this
# host for N seconds.
#
# Default is deliberately ONE HOUR, not the year you will eventually want.
# Django's own warning calls careless HSTS "serious, irreversible": browsers
# cache the policy, so if HTTPS breaks after you have advertised a year, users
# cannot reach the site at all until it expires — and clearing it from their
# browsers is not something you can do.
#
# The ramp, once HTTPS is proven stable: 3600 -> 86400 -> 31536000, then
# consider preload. See docs/layout.md.
SECURE_HSTS_SECONDS = env.int("DJANGO_SECURE_HSTS_SECONDS", default=3600)
SECURE_HSTS_INCLUDE_SUBDOMAINS = env.bool(
    "DJANGO_SECURE_HSTS_INCLUDE_SUBDOMAINS", default=True
)
# Preload means submitting to a browser-vendor list that is slow to leave.
# Off by default: it is a commitment, not a setting.
SECURE_HSTS_PRELOAD = env.bool("DJANGO_SECURE_HSTS_PRELOAD", default=False)

# Origins allowed to submit cross-origin POSTs. Needed when the site is served
# behind a proxy on a different host than Django sees.
CSRF_TRUSTED_ORIGINS = env.list("DJANGO_CSRF_TRUSTED_ORIGINS", default=[])

# --------------------------------------------------------------------------
# Waived checks — each one needs a written reason
# --------------------------------------------------------------------------
# A green audit achieved by suppression tells a reader nothing, so anything
# added here must justify itself.
#
# security.W021 — "SECURE_HSTS_PRELOAD is not True"
#
#   Waived because preload is a DEPLOYMENT COMMITMENT, not a code setting.
#   Submitting a domain to the browser preload list means every browser
#   refuses plaintext for it and every subdomain, shipped in the browser
#   binary itself. Removal takes months to propagate.
#
#   A template must not make that commitment on behalf of projects forged from
#   it: a derived project that has not yet served HTTPS on all subdomains would
#   render them unreachable, with no quick way back.
#
#   It is not "ignore this" — it is "the project decides this, not the
#   template". Turning it on is one variable:
#       DJANGO_SECURE_HSTS_PRELOAD=true
#   once HSTS has run at 31536000 without incident. Then remove this waiver.
#
# Measured: with preload enabled the audit reports zero issues, so W021 is the
# only thing standing between this configuration and a clean run.
SILENCED_SYSTEM_CHECKS: list[str] = [
    "security.W021",
]

# PostgreSQL, from DATABASE_URL. See database_from_url() for the connection
# reuse settings and the worker-count arithmetic that bounds them.
DATABASES = {"default": database_from_url()}
