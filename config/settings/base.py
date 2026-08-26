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

# The repository root. This file is config/settings/base.py, so THREE parents
# up — it moved a level deeper when settings became a package (M3-02). Getting
# this wrong points STATIC_ROOT and the database at the wrong directory.
BASE_DIR = Path(__file__).resolve().parent.parent.parent

# --------------------------------------------------------------------------
# Security
# --------------------------------------------------------------------------
# TODO(M3-03): parse this with typed environment handling that fails loudly
# when it is missing. TODO(M3-05): the fallback below must not survive into
# the production settings layer.
SECRET_KEY = os.environ.get(
    "DJANGO_SECRET_KEY",
    "dev-only-insecure-key-replaced-in-M3-03",
)

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
