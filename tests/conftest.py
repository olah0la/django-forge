"""Shared test fixtures.

pytest-django loads settings from DJANGO_SETTINGS_MODULE, which
pyproject.toml pins to config.settings.test.
"""

import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def load_settings():
    """Import a settings layer in a FRESH interpreter and report the result.

    Settings execute once at import, so a layer cannot be meaningfully
    re-imported in-process to test different environments — the first import
    wins for the life of the run. A subprocess gives each case a clean start,
    and has the side benefit of exercising real startup rather than a
    simulation of it.

    Returns (returncode, stdout, stderr). A non-zero code means the layer
    refused to start, which several tests below assert deliberately.
    """

    def _load(layer: str, env: dict[str, str], expression: str = "") -> tuple[int, str, str]:
        code = (
            "import django, os, json;"
            f"os.environ['DJANGO_SETTINGS_MODULE']='config.settings.{layer}';"
            "django.setup();"
            "from django.conf import settings;"
            f"print(json.dumps({expression or '{}'}))"
        )
        proc = subprocess.run(
            [sys.executable, "-c", code],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            # A clean environment: only what the test supplies, so a variable
            # left over from the developer's shell cannot mask a failure.
            env={
                "PATH": "/usr/bin:/bin",
                "HOME": "/tmp",
                # Point .env loading at a path that cannot exist. Without this
                # the tests read whatever .env the developer happens to have,
                # so the same commit passes on one machine and fails on
                # another — which is exactly what happened before this line.
                "DJANGO_ENV_FILE": "/nonexistent/.env",
                # base.py requires DATABASE_URL with no fallback (M4-02). This
                # is a dummy: nothing here connects, and the test layer
                # overrides DATABASES with in-memory SQLite anyway. A test that
                # needs a specific value passes its own, which wins via **env.
                "DATABASE_URL": "postgresql://forge:forge@db:5432/forge",
                **env,
            },
        )
        return proc.returncode, proc.stdout.strip(), proc.stderr.strip()

    return _load
