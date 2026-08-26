"""Test settings: fast, isolated, and never touching real infrastructure.

Selected by pyproject.toml's pytest configuration.
"""

from .base import *  # noqa: F403

DEBUG = False
ALLOWED_HOSTS = ["testserver", "localhost"]

# Fixed and obviously fake. Tests must not depend on a generated key (results
# would vary per run) nor on a real one (which would then have to exist in CI).
# Safe because this layer is never served.
SECRET_KEY = "test-only-not-a-real-key"  # noqa: S105

# In-memory database: no file on disk, no cleanup, and markedly faster.
#
# Still SQLite even though M4-01 added a PostgreSQL service, deliberately: the
# current tests cover settings and configuration and touch no database
# behaviour, so switching would make every run depend on a container for no
# added coverage.
#
# TODO(M4-04): switch to PostgreSQL when the first model or migration test
# arrives. PostgreSQL-specific behaviour — constraints, transaction semantics,
# JSON fields, collation — cannot be tested against SQLite, and a test suite
# that passes on SQLite while production runs PostgreSQL is worse than none.
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
