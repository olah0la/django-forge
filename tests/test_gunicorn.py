"""The production application server's configuration (M6-02).

`config/gunicorn.py` is read ONCE, by gunicorn, before the application is
imported — so like the settings layers it cannot be meaningfully re-imported
in-process to test a different environment. The `load_gunicorn_config` fixture
uses a subprocess for the same reason `load_settings` does, and with the same
side benefit: it exercises the real import rather than a simulation of it.

The pure helpers (`parse_cgroup_v2_cpu_max`, `default_workers`) have no
environment dependency and are imported directly.

What these guard is the reasoning, not gunicorn: that the defaults documented
in docs/serving.md are the defaults actually shipped, that every one of them is
overridable, and that the two values with a measured argument behind them —
the graceful-timeout headroom and the non-deprecated worker class — cannot be
changed without a test saying so.
"""

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

from config.gunicorn import DEFAULT_WORKER_CAP, default_workers, parse_cgroup_v2_cpu_max

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def load_gunicorn_config():
    """Import config/gunicorn.py in a FRESH interpreter and report its values.

    Returns the requested module attributes as a dict. A clean environment is
    passed, so a GUNICORN_* left over in the developer's shell cannot mask a
    failure — the same trap `load_settings` guards against with .env.
    """

    def _load(env: dict[str, str], names: tuple[str, ...]) -> dict:
        code = (
            "import json, config.gunicorn as g;"
            f"print(json.dumps({{n: getattr(g, n) for n in {names!r}}}))"
        )
        proc = subprocess.run(
            [sys.executable, "-c", code],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            env={"PATH": "/usr/bin:/bin", "HOME": "/tmp", **env},
        )
        assert proc.returncode == 0, proc.stderr
        return json.loads(proc.stdout)

    return _load


# ---------------------------------------------------------------------------
# Defaults — the numbers docs/serving.md explains
# ---------------------------------------------------------------------------
def test_defaults_are_what_the_documentation_claims(load_gunicorn_config):
    values = load_gunicorn_config(
        {},
        ("bind", "worker_class", "timeout", "graceful_timeout", "accesslog", "errorlog"),
    )
    assert values["bind"] == "0.0.0.0:8000"
    assert values["worker_class"] == "uvicorn_worker.UvicornWorker"
    assert values["timeout"] == 30
    assert values["graceful_timeout"] == 25
    # stdout, never a file: a container filesystem is ephemeral and nothing
    # rotates a log written inside one.
    assert values["accesslog"] == "-"
    assert values["errorlog"] == "-"


def test_graceful_timeout_leaves_headroom_under_the_platform_grace_period():
    """It must expire BEFORE the platform kills the container.

    30s is the common platform default — `stop_grace_period` for app-prod in
    docker-compose.yml, and Kubernetes' terminationGracePeriodSeconds. Equal
    values race: the platform can SIGKILL at the same instant gunicorn is still
    draining, and the graceful timeout never gets to do its job.
    """
    from config.gunicorn import graceful_timeout

    assert graceful_timeout < 30, (
        "graceful_timeout must stay under the 30s platform default; "
        "raise stop_grace_period first and keep the gap"
    )


def test_worker_class_is_not_the_deprecated_module(load_gunicorn_config):
    """`uvicorn.workers` emits a DeprecationWarning and is slated for removal.

    A template must not ship a deprecated import that every derived project
    inherits — hence uvicorn-worker as its own runtime dependency.
    """
    worker_class = load_gunicorn_config({}, ("worker_class",))["worker_class"]
    assert "uvicorn.workers" not in worker_class


def test_the_application_loads_the_configured_worker_class():
    """The default must be importable, not merely a plausible string.

    A typo here fails at container start with "Worker failed to boot", after
    the image has already been built and pushed.
    """
    from gunicorn.config import Config

    from config.gunicorn import worker_class

    cfg = Config()
    cfg.set("worker_class", worker_class)
    assert cfg.worker_class.__name__ == "UvicornWorker"


# ---------------------------------------------------------------------------
# Every knob is overridable
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("variable", "attribute", "value", "expected"),
    [
        ("GUNICORN_WORKERS", "workers", "4", 4),
        ("GUNICORN_WORKER_CLASS", "worker_class", "uvicorn.workers.UvicornH11Worker",
         "uvicorn.workers.UvicornH11Worker"),
        ("GUNICORN_TIMEOUT", "timeout", "90", 90),
        ("GUNICORN_GRACEFUL_TIMEOUT", "graceful_timeout", "15", 15),
        ("GUNICORN_FORWARDED_ALLOW_IPS", "forwarded_allow_ips", "10.0.0.1", "10.0.0.1"),
    ],
)
def test_environment_overrides_every_documented_knob(
    load_gunicorn_config, variable, attribute, value, expected
):
    assert load_gunicorn_config({variable: value}, (attribute,))[attribute] == expected


def test_empty_is_treated_as_unset(load_gunicorn_config):
    """An empty value must not override, and must not crash.

    The same slip config/settings/base.py's `require()` exists for: an unset
    template placeholder or a blank CI secret arrives as "". Read naively,
    `int("")` raises a ValueError naming nothing.
    """
    values = load_gunicorn_config(
        {"GUNICORN_TIMEOUT": "", "GUNICORN_WORKER_CLASS": "   "},
        ("timeout", "worker_class"),
    )
    assert values["timeout"] == 30
    assert values["worker_class"] == "uvicorn_worker.UvicornWorker"


def test_web_concurrency_is_honoured_and_gunicorn_workers_wins(load_gunicorn_config):
    """WEB_CONCURRENCY is what platforms set for you; ours overrides it.

    That ordering is what lets a platform's guess be corrected without having
    to unset a variable the platform owns.
    """
    assert load_gunicorn_config({"WEB_CONCURRENCY": "6"}, ("workers",))["workers"] == 6
    both = load_gunicorn_config(
        {"WEB_CONCURRENCY": "6", "GUNICORN_WORKERS": "2"}, ("workers",)
    )
    assert both["workers"] == 2


def test_an_explicit_worker_count_is_never_capped(load_gunicorn_config):
    """The cap guards the COMPUTED default only.

    A deployment that has sized itself against its own max_connections has
    earned the right to exceed the template's guardrail.
    """
    workers = load_gunicorn_config({"GUNICORN_WORKERS": "32"}, ("workers",))["workers"]
    assert workers == 32 > DEFAULT_WORKER_CAP


# ---------------------------------------------------------------------------
# CPU detection — os.cpu_count() is the wrong number in a container
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("200000 100000", 2.0),  # --cpus=2
        ("50000 100000", 0.5),  # half a CPU
        ("100000 100000\n", 1.0),  # trailing newline, as the file really has
        ("max 100000", None),  # no limit set — fall through to the next source
        ("", None),
        ("garbage", None),
        ("0 100000", None),
    ],
)
def test_cgroup_quota_parsing(text, expected):
    """cpu.max holds "<quota> <period>" in microseconds, or "max <period>".

    Returning None for the unlimited and unparseable cases is what makes the
    fallback chain in detect_cpus() work rather than silently yielding 1.
    """
    assert parse_cgroup_v2_cpu_max(text) == expected


def test_default_worker_count_is_capped():
    """An unconfigured container on a large host must not compute 25 workers.

    Measured: (2 × 12) + 1 = 25 on the machine this was built on, which is
    several times PostgreSQL's default max_connections once each worker is
    busy — an outage from a default nobody chose. See docs/serving.md.
    """
    assert default_workers() <= DEFAULT_WORKER_CAP


# ---------------------------------------------------------------------------
# The image runs it
# ---------------------------------------------------------------------------
def test_dockerfile_runs_gunicorn_with_this_config():
    """The config file is only reachable because the image's CMD names it.

    Exec form matters as much as the command: a shell form would leave `sh` as
    the process the entrypoint execs, so SIGTERM would reach the shell rather
    than gunicorn — the failure docker-entrypoint.sh's `exec` exists to
    prevent, reintroduced one line later.
    """
    dockerfile = (REPO_ROOT / "Dockerfile").read_text()

    assert 'CMD ["gunicorn", "config.asgi:application", "-c", "python:config.gunicorn"]' in (
        dockerfile
    )
    assert "no application in this image yet" not in dockerfile, (
        "the M6-02 placeholder CMD is still present"
    )


def test_production_profile_does_not_override_the_command():
    """app-prod must run the image as built.

    An override in Compose means the thing verified locally is not the thing
    that ships — the same reason that service has no source mount.
    """
    compose = (REPO_ROOT / "docker-compose.yml").read_text()

    def command_keys(text: str) -> list[str]:
        # Real YAML keys only. The block that explains why app-prod has no
        # `command:` says the word repeatedly, and matching a comment would
        # make this test fail on its own documentation.
        return [
            line for line in text.splitlines()
            if re.match(r"^\s+command:", line) and not line.lstrip().startswith("#")
        ]

    _, _, app_prod = compose.partition("  app-prod:")
    assert app_prod, "app-prod service not found"
    assert command_keys(app_prod) == []
    # The development service keeps its uvicorn --reload command, so the
    # absence above has to be specific to app-prod rather than to the file.
    assert command_keys(compose)
