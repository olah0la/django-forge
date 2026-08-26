"""Project configuration package."""

import os

# The layers a caller may select. Listed here so the error message below can
# name them, rather than leaving someone to guess at module paths.
SETTINGS_MODULES = (
    "config.settings.development",
    "config.settings.production",
    "config.settings.test",
)


def require_settings_module() -> str:
    """Return DJANGO_SETTINGS_MODULE, or fail loudly if it is not set.

    Deliberately NOT `os.environ.setdefault(...)`. A default means a missing
    variable in production silently yields whatever layer was hard-coded as the
    fallback — debug on, permissive hosts, and no indication anything is wrong.
    Refusing to start is strictly better than starting wrong.

    Defined once and called from manage.py, wsgi.py and asgi.py so the three
    entry points cannot drift apart.
    """
    module = os.environ.get("DJANGO_SETTINGS_MODULE", "").strip()
    if not module:
        raise SystemExit(
            "\n  DJANGO_SETTINGS_MODULE is not set.\n\n"
            "  This project has no default settings layer on purpose: guessing one\n"
            "  is how a production process ends up running development settings.\n\n"
            "  Choose one:\n"
            + "".join(f"      {m}\n" for m in SETTINGS_MODULES)
            + "\n  For example:\n"
            "      DJANGO_SETTINGS_MODULE=config.settings.development python manage.py check\n\n"
            "  Both Compose profiles set this for you; see docker-compose.yml.\n"
        )
    return module
