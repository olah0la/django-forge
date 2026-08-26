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
# TODO(M4-01): PostgreSQL-specific behaviour (constraints, transactions, JSON
# fields) cannot be tested against SQLite. Revisit when the db service exists.
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
