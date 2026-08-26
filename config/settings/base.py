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
# TODO(M4-01/M4-02): PostgreSQL is the real target. SQLite is a placeholder so
# the project runs before the database service exists — it is NOT the intended
# engine, and developing against it hides transaction and constraint
# differences that then surface only in production.
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": BASE_DIR / "db.sqlite3",
    }
}

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
