"""Settings and environment-parsing behaviour.

The first test here is the one tradeoffs.local.md entry 34 named as the test to
write first: the boolean conversion of the literal string "False".
"""

import json
import re
from pathlib import Path

import pytest
from django.conf import settings

REPO_ROOT = Path(__file__).resolve().parent.parent
SETTINGS_DIR = REPO_ROOT / "config" / "settings"


# ---------------------------------------------------------------------------
# The trap: "False" is a non-empty string
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("value", ["False", "false", "FALSE", "0", "no", "off"])
def test_falsey_strings_parse_as_false(load_settings, value):
    """DJANGO_DEBUG="False" must be False, not truthy.

    This is the bug the typed reader exists to prevent: read raw, "False" is a
    non-empty string and therefore truthy, so debug mode is silently ON in
    production — exposing tracebacks, settings and query fragments.
    """
    rc, out, err = load_settings(
        "development", {"DJANGO_DEBUG": value}, "{'debug': settings.DEBUG}"
    )
    assert rc == 0, err
    assert json.loads(out)["debug"] is False, f"DJANGO_DEBUG={value!r} was treated as true"


@pytest.mark.parametrize("value", ["True", "true", "1", "yes", "on"])
def test_truthy_strings_parse_as_true(load_settings, value):
    rc, out, err = load_settings(
        "development", {"DJANGO_DEBUG": value}, "{'debug': settings.DEBUG}"
    )
    assert rc == 0, err
    assert json.loads(out)["debug"] is True


# ---------------------------------------------------------------------------
# Required variables fail loudly, naming themselves
# ---------------------------------------------------------------------------
def test_production_without_secret_key_fails_naming_it(load_settings):
    rc, _, err = load_settings("production", {"DJANGO_ALLOWED_HOSTS": "example.com"})
    assert rc != 0, "production started without DJANGO_SECRET_KEY"
    assert "DJANGO_SECRET_KEY" in err, f"error did not name the variable: {err}"


def test_production_without_allowed_hosts_fails_naming_it(load_settings):
    rc, _, err = load_settings("production", {"DJANGO_SECRET_KEY": "x" * 50})
    assert rc != 0, "production started without DJANGO_ALLOWED_HOSTS"
    assert "DJANGO_ALLOWED_HOSTS" in err


@pytest.mark.parametrize("value", ["1", "true", "yes", "on"])
def test_production_refuses_to_enable_debug(load_settings, value):
    rc, _, err = load_settings(
        "production",
        {
            "DJANGO_SECRET_KEY": "x" * 50,
            "DJANGO_ALLOWED_HOSTS": "example.com",
            "DJANGO_DEBUG": value,
        },
    )
    assert rc != 0, f"production accepted DJANGO_DEBUG={value!r}"
    assert "DEBUG cannot be enabled" in err


# ---------------------------------------------------------------------------
# Typed conversion: values arrive as types, not strings
# ---------------------------------------------------------------------------
def test_allowed_hosts_parses_as_a_list(load_settings):
    rc, out, err = load_settings(
        "production",
        {
            "DJANGO_SECRET_KEY": "x" * 50,
            "DJANGO_ALLOWED_HOSTS": "a.example.com,b.example.com",
        },
        "{'hosts': settings.ALLOWED_HOSTS}",
    )
    assert rc == 0, err
    assert json.loads(out)["hosts"] == ["a.example.com", "b.example.com"]


# ---------------------------------------------------------------------------
# No insecure defaults anywhere
# ---------------------------------------------------------------------------
def test_no_hardcoded_secret_key_outside_the_test_layer():
    """A committed key would be identical in every derived project."""
    offenders = []
    for path in SETTINGS_DIR.glob("*.py"):
        if path.name == "test.py":  # fixed, obviously fake, never served
            continue
        for line in path.read_text().splitlines():
            if re.match(r"\s*SECRET_KEY\s*=\s*['\"]", line):
                offenders.append(f"{path.name}: {line.strip()}")
    assert not offenders, f"hard-coded SECRET_KEY found: {offenders}"


def test_development_generates_a_key_when_none_is_supplied(load_settings):
    """No .env, no variable — development must still start, on a random key."""
    rc, out, err = load_settings(
        "development", {}, "{'length': len(settings.SECRET_KEY)}"
    )
    assert rc == 0, err
    assert json.loads(out)["length"] >= 32


def test_generated_keys_differ_between_runs(load_settings):
    """Confirms the key is generated, not a constant in disguise."""
    expr = "{'key': settings.SECRET_KEY}"
    _, first, _ = load_settings("development", {}, expr)
    _, second, _ = load_settings("development", {}, expr)
    assert json.loads(first)["key"] != json.loads(second)["key"]


# ---------------------------------------------------------------------------
# .env.example is the contract, and must stay honest
# ---------------------------------------------------------------------------
def test_env_example_exists_and_holds_no_real_secret():
    example = (REPO_ROOT / ".env.example").read_text()
    assert "DJANGO_SECRET_KEY=" in example
    # The key placeholder must be empty; a value here would be a committed key.
    assert re.search(r"^DJANGO_SECRET_KEY=\s*$", example, re.M), (
        ".env.example must ship DJANGO_SECRET_KEY empty"
    )


def test_test_layer_uses_an_in_memory_database():
    assert settings.DATABASES["default"]["NAME"] == ":memory:"


# ---------------------------------------------------------------------------
# Empty is not the same as set
# ---------------------------------------------------------------------------
# Regression tests for a real gap found during M3-03: django-environ raises
# only when a variable is ABSENT. Present-but-empty passed its check, so
# DJANGO_ALLOWED_HOSTS="" booted the application with ALLOWED_HOSTS = [] and
# every request would have been rejected with no explanation. Setting a
# variable to the empty string is a common deployment slip.
@pytest.mark.parametrize(
    "supplied",
    [
        {"DJANGO_SECRET_KEY": "", "DJANGO_ALLOWED_HOSTS": "example.com"},
        {"DJANGO_SECRET_KEY": "x" * 50, "DJANGO_ALLOWED_HOSTS": ""},
        {"DJANGO_SECRET_KEY": "   ", "DJANGO_ALLOWED_HOSTS": "example.com"},
    ],
    ids=["empty-secret", "empty-hosts", "whitespace-secret"],
)
def test_empty_required_variables_are_treated_as_missing(load_settings, supplied):
    rc, _, err = load_settings("production", supplied)
    assert rc != 0, f"production started with {supplied}"
    assert "is required but is empty or unset" in err, err
