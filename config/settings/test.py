"""Test settings: fast and isolated by default, faithful on demand.

Selected by pyproject.toml's pytest configuration. The database is SQLite
unless DJANGO_TEST_DATABASE_URL is set — see the DATABASES block below.
"""

# NOTE: importing base requires DATABASE_URL, because the base layer has no
# fallback on purpose. conftest.py supplies a dummy value, and the DATABASES
# block below replaces it entirely — the test database is chosen by
# DJANGO_TEST_DATABASE_URL, never by DATABASE_URL.
from .base import *  # noqa: F403

DEBUG = False
ALLOWED_HOSTS = ["testserver", "localhost"]

# Fixed and obviously fake. Tests must not depend on a generated key (results
# would vary per run) nor on a real one (which would then have to exist in CI).
# Safe because this layer is never served.
SECRET_KEY = "test-only-not-a-real-key"  # noqa: S105

# Two engines, chosen by one variable.
#
#   unset  ->  in-memory SQLite. `make test` on the host: fast, no container,
#              no cleanup. Covers settings, configuration, and any model
#              behaviour that is not engine-specific.
#   set    ->  that database. `make test-db` inside the container: the real
#              engine, for constraints, transaction semantics, JSON fields and
#              collation — none of which SQLite can answer for.
#
# WHY A DEDICATED VARIABLE AND NOT `DATABASE_URL`. Reusing it would mean any
# developer with one in their `.env` silently redirects the whole suite at a
# database port that is deliberately not published (M4-01), and `make test`
# starts failing on their machine and nowhere else. That is not hypothetical:
# it is exactly what happened in M3-05, when the suite began reading the
# developer's `.env` and six tests failed for one person only. This variable is
# set by `make test-db` and by nothing else.
#
# The fallback is safe here in a way it would NOT be in base.py, where a silent
# SQLite default would let production commands run against the wrong engine.
# The test layer never serves traffic, and the engine it used is visible in the
# name of the command you ran.
if env.str("DJANGO_TEST_DATABASE_URL", default="").strip():  # noqa: F405
    DATABASES = {"default": env.db_url("DJANGO_TEST_DATABASE_URL")}  # noqa: F405
else:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": ":memory:",
        }
    }

# The default hasher is deliberately slow to resist brute force. That is exactly
# wrong for tests, where it dominates the runtime of anything creating a user.
PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]

EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"

# Enabled so the seed command's own tests can call it. The guard itself is
# tested by overriding this to False, which is the case that matters.
SEED_ENABLED = True

# A test-only app holding concrete models to exercise the abstract bases in
# apps/core/models.py. Abstract models create no table and cannot be queried,
# so the behaviour they provide can only be tested through a subclass.
#
# Registered HERE and nowhere else: these models must never reach a real
# database. The app has no migrations, so Django creates its tables directly
# during test-database setup.
INSTALLED_APPS = [*INSTALLED_APPS, "tests.testapp"]  # noqa: F405
