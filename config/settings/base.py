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

# Safe to import from a settings module, and the only project import here.
# apps/core/logging.py holds nothing but standard library — see the warning in
# its docstring about why it must stay that way.
from apps.core.logging import CONSOLE_DATE_FORMAT, CONSOLE_FORMAT

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
    # NOT required to route the API — django-ninja is wired through URLs
    # (config/api.py, mounted by config/urls.py), and everything answers
    # correctly without this entry. It is here for one reason: the interactive
    # documentation page.
    #
    # Ninja checks for "ninja" in INSTALLED_APPS when rendering /api/v1/docs.
    # Present, it renders from the 7.9 MB of Swagger UI assets bundled in the
    # package. Absent, it falls back to a template that loads swagger-ui from
    # cdn.jsdelivr.net — so the page breaks with no network and every developer
    # opening it makes a request to a third party.
    #
    # Removing this looks safe, because the API keeps working. See docs/api.md.
    "ninja",
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
    # FIRST, on purpose. Middleware wraps in list order, so the first entry is
    # the outermost one: the correlation identifier exists before any other
    # middleware runs, and the response header is attached after all of them
    # have finished. Moved further down, every line logged by the middleware
    # above it would be uncorrelated — including the ones from a request that
    # was rejected before reaching a view, which are the ones you most want to
    # be able to find. See docs/logging.md.
    "apps.core.middleware.RequestIDMiddleware",
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
    #
    # The other half of the multiplication is the WORKER COUNT, which M6-02
    # made configurable — config/gunicorn.py holds it, and docs/serving.md
    # works the arithmetic through with numbers for both halves.
    config["CONN_MAX_AGE"] = env.int("DJANGO_CONN_MAX_AGE", default=60)

    # A pooled connection can be killed by a database restart or a network
    # blip. Without this, the next request to reuse the dead connection fails
    # with an InterfaceError that has no obvious cause; Django instead runs a
    # cheap liveness check and replaces it.
    #
    # This failure mode exists BECAUSE CONN_MAX_AGE > 0, so the two ship together.
    config["CONN_HEALTH_CHECKS"] = config["CONN_MAX_AGE"] > 0

    return config


# --------------------------------------------------------------------------
# Logging (M6-04)
# --------------------------------------------------------------------------
# LOGGING itself is NOT set here, for the reason at the top of this file: the
# format is environment-specific. Base defines the BUILDER and each layer calls
# it with the format that layer wants — the same shape as database_from_url()
# above.
#
# Everything goes to stdout. No file handler, ever: a log file written inside a
# container is deleted with the container, and the one time you want it is
# after the container is gone.

# How much is emitted. The one logging knob that is genuinely operational —
# turning up detail to investigate a live incident should not need a deploy.
LOG_LEVEL = env.str("DJANGO_LOG_LEVEL", default="INFO").upper()

# The header the correlation identifier is read from and returned in.
#
# NOT environment-readable. It is part of the contract between this service and
# whatever calls it — change it on one side only and correlation silently stops
# working across the boundary, with nothing failing. `X-Request-ID` is the de
# facto name, understood by most proxies and log collectors already.
REQUEST_ID_HEADER = "X-Request-ID"

# Paths that get an identifier and a response header but no request log line.
#
# TODO(M6-01): the liveness and readiness endpoints belong here. An orchestrator
# probes them every few seconds for the life of every container, which is tens
# of thousands of identical lines a day burying everything of interest.
#
# Deliberately EMPTY until M6-01 chooses their paths — that issue owns its own
# URL contract, and a guess here would either conflict with it or, worse, agree
# with it by accident and look verified when it was not.
REQUEST_LOG_EXCLUDED_PATHS: list[str] = []

LOG_FORMATS = ("json", "console")


def build_logging(fmt: str) -> dict:
    """Build Django's LOGGING dictionary for one output format.

    `json` for production — one object per line, which turns "every failed
    request for this user in this window" into a query. `console` for
    development, because reading JSON by eye all day is its own punishment.

    Development can be run with DJANGO_LOG_FORMAT=json. That is not a
    curiosity: two formats means the format that matters is the one nobody
    looks at until it is deployed, and this is how that gets checked first.
    """
    if fmt not in LOG_FORMATS:
        raise ImproperlyConfigured(
            f"DJANGO_LOG_FORMAT={fmt!r} is not a known format. "
            f"Choose one of: {', '.join(LOG_FORMATS)}."
        )

    return {
        "version": 1,
        # Loggers already created by imports that ran before this — every
        # module-level `logging.getLogger(__name__)` in Django and in every
        # dependency — would otherwise be switched OFF. The default for this
        # key is True, and it is one of the great silent log-loss bugs.
        "disable_existing_loggers": False,
        "filters": {
            "request_id": {"()": "apps.core.logging.RequestIDFilter"},
        },
        "formatters": {
            "json": {"()": "apps.core.logging.JSONFormatter"},
            "console": {
                "format": CONSOLE_FORMAT,
                "datefmt": CONSOLE_DATE_FORMAT,
            },
        },
        "handlers": {
            "console": {
                "class": "logging.StreamHandler",
                "stream": "ext://sys.stdout",
                "formatter": fmt,
                # On the HANDLER, so every record reaching it is correlated no
                # matter which logger produced it. See apps/core/logging.py.
                "filters": ["request_id"],
            },
        },
        # The one handler lives here and everything propagates to it. That is
        # why none of the loggers below declare handlers of their own: a logger
        # WITH a handler that also propagates prints every line twice.
        "root": {"handlers": ["console"], "level": LOG_LEVEL},
        "loggers": {
            # Django applies its own DEFAULT_LOGGING first and this dictionary
            # second, so naming a logger here REPLACES what Django gave it —
            # including the `mail_admins` handler on `django`, which this
            # project does not configure and must not silently inherit.
            "django": {"level": LOG_LEVEL},
            # Pinned at INFO, and NOT following LOG_LEVEL. At DEBUG this logger
            # prints every SQL statement WITH ITS BOUND PARAMETERS — the widest
            # secret leak available in this configuration, and it is one
            # environment variable away in every other project that wires
            # logging without noticing. Turning it on is a deliberate local
            # edit, described in docs/logging.md, not a side effect of asking
            # for more detail.
            "django.db.backends": {"level": "INFO"},
            # runserver's logger. This project never runs it, but leaving
            # Django's default in place would mean the one command that does
            # emits a differently-formatted line.
            "django.server": {"level": "INFO", "propagate": True},
            # Project code. `apps.request` — the request log — is under it.
            "apps": {"level": LOG_LEVEL},
            # Uvicorn's own startup and error output, reformatted rather than
            # silenced: "Application startup complete" and a failed bind belong
            # in the same stream as everything else.
            "uvicorn": {"level": "INFO", "propagate": True},
            "uvicorn.error": {"level": "INFO", "propagate": True},
            # SILENCED, and this is a decision rather than noise reduction.
            # RequestIDMiddleware already logs one line per request, and it is
            # the only layer that can see the correlation identifier. Leaving
            # this on gives two lines per request, one of them uncorrelated.
            # TODO(M6-02): Gunicorn's access log needs the same treatment for
            # the same reason — see docs/logging.md.
            "uvicorn.access": {"handlers": [], "level": "INFO", "propagate": False},
        },
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

# --------------------------------------------------------------------------
# API (M5)
# --------------------------------------------------------------------------
# Project metadata, not environment configuration: the API is called the same
# thing wherever it runs, which is why these live in base rather than in a
# layer. They are environment-READABLE so a derived project can rename the API
# without editing code — a template whose name is hard-coded is a template that
# every adopter has to patch.
#
# These become the OpenAPI document's `info` block, so they are what a consumer
# reads first. API_VERSION is the DOCUMENT version and is not the `v1` in the
# URL — config/api.py explains why the two must not be tied together.
API_TITLE = env.str("DJANGO_API_TITLE", default="django-forge API")
API_VERSION = env.str("DJANGO_API_VERSION", default="1.0.0")
API_DESCRIPTION = env.str(
    "DJANGO_API_DESCRIPTION",
    default="HTTP API for a project forged from django-forge.",
)

# Whether /api/v1/docs and the OpenAPI schema are served.
#
# OFF here, so production inherits the safe value and never has to remember
# anything — the same reasoning as SEED_ENABLED below. The docs page is a
# complete, machine-readable description of every endpoint, parameter and
# response shape, which is exactly what an attacker enumerating an API wants.
#
# UNLIKE SEED_ENABLED, the production layer does offer an environment override.
# Seeding in production is always a mistake; published API docs are a normal
# choice for a public API. The default is the cautious one, and turning it on
# is deliberate rather than forbidden.
API_DOCS_ENABLED = False

# --------------------------------------------------------------------------
# Pagination (M5-04)
# --------------------------------------------------------------------------
# Read by django-ninja, so the names are ITS names, not ours. All four are read
# ONCE, when `ninja.conf` is imported, and the maximum is baked into the query
# parameter's validation at class-definition time. That means `override_settings`
# cannot change them in a test — the same trap as the API instance in
# config/api.py. Tests assert the real, configured behaviour instead.
#
# Not environment-readable: a page ceiling is a property of the API's contract,
# published in the OpenAPI document, and one stray environment variable should
# not be able to move it. See docs/api.md.
NINJA_PAGINATION_CLASS = "ninja.pagination.LimitOffsetPagination"

# What a client gets when it asks for no particular size.
NINJA_PAGINATION_PER_PAGE = 25

# THE LOAD-BEARING LINE. Ninja's default for this is `inf` — with no value here,
# `?limit=1000000` is a valid request and the endpoint tries to serve it, which
# is precisely the failure pagination exists to prevent. Requests above the
# ceiling are REFUSED with 422 rather than quietly clamped: a client that
# believes it received 1,000 rows and received 100 will page through the data
# wrongly and never know.
NINJA_PAGINATION_MAX_LIMIT = 100

# The same ceiling for the other paginators. Nothing reads it today —
# LimitOffsetPagination uses MAX_LIMIT above — and it is set so that switching
# to PageNumberPagination or CursorPagination inherits a limit instead of
# silently losing one.
#
# The missing `PAGINATION_` in the name is NOT a typo here: Ninja's alias for
# this setting really is `NINJA_MAX_PER_PAGE_SIZE`, alone among the four.
# "Correcting" it to NINJA_PAGINATION_MAX_PER_PAGE_SIZE means Ninja never reads
# it, and nothing fails — the ceiling just quietly reverts to the default.
NINJA_MAX_PER_PAGE_SIZE = 100

# --------------------------------------------------------------------------
# Seed data
# --------------------------------------------------------------------------
# `manage.py seed` refuses to run unless this is True. OFF here, so production
# inherits the safe value and never has to remember anything — the development
# and test layers opt in.
#
# NOT read from the environment, deliberately. A .env is read by BOTH Compose
# profiles (see docs/layout.md, "Layer-specific values do not belong in .env"),
# so an env-readable flag would be one stray SEED_ENABLED=1 away from arming
# the exact thing it exists to prevent. That is not hypothetical: the same
# section records DJANGO_SETTINGS_MODULE in a .env silently making the
# production-like profile run development settings.
#
# A layer sets this in code or it does not get it.
SEED_ENABLED = False

# --------------------------------------------------------------------------
# Health checks (M6-01)
# --------------------------------------------------------------------------
# The two probe paths, written down ONCE. Three other things derive from this
# tuple — the URLconf, the SSL-redirect exemption below, and the access-log
# filter — so the paths cannot drift apart between them.
#
# Deliberately NOT under /api/v1/. A probe URL is an infrastructure contract
# consumed by Docker and orchestrators; the API prefix is a contract boundary
# for API clients, and a future v2 must not be able to move a path that lives in
# deployment manifests. See apps/core/health.py and docs/ops.md.
#
# Not environment-readable: an orchestrator's probe configuration and this
# tuple have to agree, and a stray variable that silently 404s every probe would
# take the whole service out of rotation.
HEALTH_CHECK_PATHS = ("/healthz", "/readyz")

# Probes reach the application over PLAIN HTTP from inside the container, so
# with SECURE_SSL_REDIRECT on they would receive a 301 to https:// and the
# container would report permanently unhealthy — a failure that looks exactly
# like a broken application and is not.
#
# Here rather than in production.py: any layer that ever turns the redirect on
# needs the exemption, and it is inert wherever the redirect is off.
#
# Django matches these against the path WITHOUT its leading slash, and they are
# anchored so nothing else can be caught by them.
SECURE_REDIRECT_EXEMPT = [rf"^{path.lstrip('/')}$" for path in HEALTH_CHECK_PATHS]

# --------------------------------------------------------------------------
# Logging (M6-01; TODO(M6-04) makes it structured)
# --------------------------------------------------------------------------
# Deliberately minimal. This exists for ONE acceptance criterion — the health
# endpoints are excluded from request logging — and M6-04 grows it into the real
# configuration: JSON in production, human-readable in development, a
# correlation identifier on every request-scoped line, and a level read from the
# environment.
#
# WHY IT TAKES OWNERSHIP OF `uvicorn.access` INSTEAD OF ONLY ADDING A FILTER.
# dictConfig clears a named logger's existing handlers, so an entry declaring
# only `filters` would remove uvicorn's own handler and silence access logging
# completely — passing the criterion by deleting the log. The handler is
# therefore declared here explicitly.
#
# The ordering that makes this work: uvicorn configures logging when its Config
# is built, THEN imports config/asgi.py, which calls django.setup() and applies
# this dict. Ours runs last and wins.
#
# What is lost, knowingly: uvicorn's colourised "INFO:" prefix. The record's
# message already carries client, method, path, version and status, which is the
# whole line. M6-04 replaces this formatter with a JSON one anyway.
#
# TODO(M6-02): gunicorn emits its own access log through `gunicorn.access`. The
# same filter has to be pointed at that logger, or the probes reappear in
# production the moment gunicorn lands.
LOGGING = {
    "version": 1,
    # Never True. It would disable every logger configured before this dict is
    # applied — which, under uvicorn, is all of them.
    "disable_existing_loggers": False,
    "filters": {
        "suppress_health_checks": {
            "()": "config.logging.SuppressHealthCheckAccessLogs",
        },
    },
    "formatters": {
        "access": {"format": "%(message)s"},
    },
    "handlers": {
        # stdout, not stderr: an access log is not an error stream, and
        # container platforms collect both.
        "access": {
            "class": "logging.StreamHandler",
            "stream": "ext://sys.stdout",
            "formatter": "access",
            "filters": ["suppress_health_checks"],
        },
    },
    "loggers": {
        "uvicorn.access": {
            "handlers": ["access"],
            "level": "INFO",
            # Otherwise every access line is emitted twice: once here and once
            # by the root logger.
            "propagate": False,
        },
        # The non-obvious half of the criterion, found by running the server and
        # reading the log rather than by reasoning about it. Django logs every
        # 4xx and 5xx through `django.request`, so during a database outage
        # EVERY readiness probe emits "Service Unavailable: /readyz" at ERROR,
        # from every replica, for the duration — on top of the deliberate,
        # detailed warning apps/core/health.py already logs.
        #
        # The filter sits on the LOGGER, not on a handler: this logger has none
        # of its own and propagates to Django's, and a filter that returns False
        # on a logger stops propagation too. No `handlers` key, deliberately —
        # declaring one would clear what it inherits.
        #
        # Records carrying an exception are exempt inside the filter, so a real
        # crash in a probe view still reaches the log with its traceback.
        "django.request": {
            "filters": ["suppress_health_checks"],
        },
    },
}
