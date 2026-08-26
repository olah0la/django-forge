"""Shared settings — the base every environment layer builds on.

**Nothing here may be environment-specific.** If a value differs between
development and production, it belongs in the layer that needs it, not behind
an `if DEBUG:` branch here. That rule is what keeps a development-only
convenience from silently applying in production.

Layers: `development.py`, `production.py`, `test.py` each do
`from .base import *` and then override explicitly. One hop, so a reader can
answer "which value wins?" by looking at exactly two files.

Values marked TODO are knowingly provisional and name the issue that resolves
them.
"""

import os
from pathlib import Path

import environ
from django.core.exceptions import ImproperlyConfigured

# The repository root. This file is config/settings/base.py, so THREE parents
# up — it moved a level deeper when settings became a package (M3-02). Getting
# this wrong points STATIC_ROOT and the database at the wrong directory.
BASE_DIR = Path(__file__).resolve().parent.parent.parent

# --------------------------------------------------------------------------
# Environment reader
# --------------------------------------------------------------------------
# Environment variables are always STRINGS. Reading them raw produces the
# classic bug: DJANGO_DEBUG="False" is a non-empty string, so a naive
# `bool(os.environ.get(...))` is True and debug mode is silently on in
# production — exposing tracebacks, settings and query fragments to anyone who
# triggers an error.
#
# `env` converts and validates instead. Declared types are enforced, and a
# required variable with no default raises ImproperlyConfigured naming itself.
env = environ.Env()

# Load a local .env if one exists. Optional on purpose: a clean checkout must
# run with no setup (M2-04), and Compose already injects the environment. This
# is what makes host-side `python manage.py ...` work too.
#
# The path is overridable, and pointing it at a non-existent file disables the
# load entirely. That is not a hypothetical convenience — the test suite needs
# it. Without it, tests read whatever .env the developer happens to have, so
# they pass on one machine and fail on another, and CI disagrees with both.
_env_file = Path(os.environ.get("DJANGO_ENV_FILE", BASE_DIR / ".env"))
if _env_file.is_file():
    env.read_env(_env_file)


def require(name: str) -> str:
    """Return a required variable's value, treating empty as missing.

    django-environ raises only when a variable is ABSENT. A variable that is
    present but empty — `DJANGO_ALLOWED_HOSTS=` — passes its check and yields
    an empty value, which is worse than failing: the application boots
    misconfigured. Measured before this helper existed: an empty
    DJANGO_ALLOWED_HOSTS produced `ALLOWED_HOSTS = []`, so every request would
    have been rejected with no indication why.

    Setting a variable to the empty string is a common deployment slip (an
    unset template placeholder, a blank CI secret), so it is treated as the
    mistake it is.
    """
    value = env.str(name, default="").strip()
    if not value:
        raise ImproperlyConfigured(
            f"{name} is required but is empty or unset. "
            "Set it in the environment or in .env — see .env.example."
        )
    return value

# --------------------------------------------------------------------------
# Security
# --------------------------------------------------------------------------
# SECRET_KEY is NOT set here. It is required in production and generated in
# development, so each layer supplies its own — a shared default in this file
# would be exactly the committed insecure constant this project refuses to ship.

# DEBUG and ALLOWED_HOSTS are deliberately NOT set here. They differ per
# environment, and a default in this file would be a default that silently
# applies in production. Each layer sets them explicitly.

# --------------------------------------------------------------------------
# Applications
# --------------------------------------------------------------------------
DJANGO_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
]

THIRD_PARTY_APPS: list[str] = [
    # django-ninja needs no entry in INSTALLED_APPS; it is wired through URLs
    # instead. M5-01 mounts the API.
]

# Project apps. Every one lives under `apps/` and is referenced by its full
# dotted path — see docs/layout.md.
LOCAL_APPS = [
    "apps.core",
]

# Kept as three lists so it is obvious at a glance which apps are ours. Django
# only ever sees the concatenation.
INSTALLED_APPS = DJANGO_APPS + THIRD_PARTY_APPS + LOCAL_APPS

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"
# TODO(M3-04): ASGI is the intended interface (django-ninja supports async
# endpoints). M3-04 configures it properly and makes it the served entrypoint.
ASGI_APPLICATION = "config.asgi.application"

# --------------------------------------------------------------------------
# Database
# --------------------------------------------------------------------------
# DATABASES is NOT set here. The development and production layers build it
# from DATABASE_URL via database_from_url() below; the test layer defines its
# own in-memory SQLite and never needs a URL.
#
# Requiring it in base would force a database URL on a test suite that does not
# use one — and pytest-django imports settings during startup, so it would fail
# before a single test ran.


def database_from_url() -> dict:
    """Build Django's DATABASES["default"] from DATABASE_URL.

    One connection URL rather than five separate variables: most deployment
    platforms hand you a URL, and five variables are five chances to configure
    a partially-wrong database. The URL embeds the password, so it must never
    be logged.

    REQUIRED, with no fallback. Falling back to SQLite would let host-side
    commands run against a DIFFERENT ENGINE than the container, silently — the
    local-vs-production divergence PostgreSQL was adopted to remove,
    reintroduced through the back door.
    """
    if not env.str("DATABASE_URL", default="").strip():
        raise ImproperlyConfigured(
            "DATABASE_URL is not set.\n\n"
            "  There is deliberately no fallback: defaulting to SQLite would run\n"
            "  your commands against a different database engine than the\n"
            "  container uses, without saying so.\n\n"
            "  Django commands run inside the container, where Compose supplies it:\n"
            "      make migrate          apply migrations\n"
            "      make django-shell     Django REPL\n"
            "      make db-shell         psql session\n"
            "      make shell            a shell in the app container\n\n"
            "  To run against the database from the host, publish its port with a\n"
            "  docker-compose.override.yml and set DATABASE_URL in .env — see\n"
            "  .env.example."
        )

    config = env.db_url("DATABASE_URL")

    # ⚠️  THE ARITHMETIC THAT MATTERS
    #
    #     total connections ≈ workers × THREADS PER WORKER
    #
    # Not workers alone. Under ASGI, sync views run in a threadpool and Django
    # connections are THREAD-LOCAL, so each busy thread holds its own. Measured
    # on this stack with CONN_MAX_AGE=60: 20 concurrent requests to a single
    # uvicorn process held 20 connections, against a max_connections of 100.
    #
    # Django's default is a new connection per request, closed at the end:
    # always correct, and wasteful — establishing a PostgreSQL connection costs
    # a round trip plus authentication, every request.
    #
    # CONN_MAX_AGE reuses one instead. But connections are held PER WORKER.
    # PostgreSQL's default max_connections is 100, and exceeding it refuses new
    # connections outright — the most common way a well-intentioned worker
    # increase causes an outage. At scale the answer is PgBouncer, NOT a larger
    # max_connections: each connection costs memory server-side.
    #
    # 60s is a deliberate middle: long enough that the per-request cost
    # disappears, short enough that idle connections are not held for minutes
    # per worker. See docs/layout.md.
    config["CONN_MAX_AGE"] = env.int("DJANGO_CONN_MAX_AGE", default=60)

    # A pooled connection can be killed by a database restart or a network
    # blip. Without this, the next request to reuse the dead connection fails
    # with an InterfaceError that has no obvious cause; Django instead runs a
    # cheap liveness check and replaces it.
    #
    # This failure mode exists BECAUSE CONN_MAX_AGE > 0, so the two ship together.
    config["CONN_HEALTH_CHECKS"] = config["CONN_MAX_AGE"] > 0

    return config


AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

# --------------------------------------------------------------------------
# Internationalisation
# --------------------------------------------------------------------------
LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
# Store datetimes in UTC. Converting for display is a presentation concern;
# storing local times is a bug that only appears at a daylight-saving boundary.
USE_TZ = True

# --------------------------------------------------------------------------
# Static files
# --------------------------------------------------------------------------
# TODO(M6-03): collectstatic runs at image build time and the serving strategy
# is decided there.
STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
