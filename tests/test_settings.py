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


# ---------------------------------------------------------------------------
# Production hardening (M3-05)
# ---------------------------------------------------------------------------
PROD_ENV = {
    "DJANGO_SECRET_KEY": "x" * 60,
    "DJANGO_ALLOWED_HOSTS": "example.com",
    "DJANGO_DEBUG": "0",
}


@pytest.mark.parametrize(
    ("setting", "expected"),
    [
        ("SESSION_COOKIE_SECURE", True),
        ("CSRF_COOKIE_SECURE", True),
        ("SECURE_SSL_REDIRECT", True),
        ("SECURE_HSTS_INCLUDE_SUBDOMAINS", True),
    ],
)
def test_production_transport_security(load_settings, setting, expected):
    rc, out, err = load_settings("production", PROD_ENV, f"{{'v': settings.{setting}}}")
    assert rc == 0, err
    assert json.loads(out)["v"] is expected


def test_hsts_default_is_conservative(load_settings):
    """One hour, not one year.

    Browsers cache HSTS. Advertising a year before HTTPS is proven stable can
    make the site unreachable for a year, and it cannot be cleared remotely.
    """
    rc, out, err = load_settings("production", PROD_ENV, "{'v': settings.SECURE_HSTS_SECONDS}")
    assert rc == 0, err
    assert json.loads(out)["v"] == 3600


def test_hsts_preload_is_not_enabled_by_default(load_settings):
    """Preload is a commitment a template must not make for derived projects."""
    rc, out, err = load_settings("production", PROD_ENV, "{'v': settings.SECURE_HSTS_PRELOAD}")
    assert rc == 0, err
    assert json.loads(out)["v"] is False


def test_proxy_ssl_header_is_opt_in(load_settings):
    """Trusting X-Forwarded-Proto unconditionally is itself a vulnerability."""
    expr = "{'v': getattr(settings, 'SECURE_PROXY_SSL_HEADER', None)}"
    rc, out, err = load_settings("production", PROD_ENV, expr)
    assert rc == 0, err
    assert json.loads(out)["v"] is None

    rc, out, err = load_settings(
        "production", {**PROD_ENV, "DJANGO_TRUST_PROXY_SSL_HEADER": "true"}, expr
    )
    assert rc == 0, err
    assert json.loads(out)["v"] == ["HTTP_X_FORWARDED_PROTO", "https"]


def test_ssl_redirect_can_be_disabled_for_load_balancers(load_settings):
    rc, out, err = load_settings(
        "production",
        {**PROD_ENV, "DJANGO_SECURE_SSL_REDIRECT": "false"},
        "{'v': settings.SECURE_SSL_REDIRECT}",
    )
    assert rc == 0, err
    assert json.loads(out)["v"] is False


def test_only_w021_is_silenced_and_it_is_justified(load_settings):
    """Every silenced check must carry a written reason in the source."""
    rc, out, err = load_settings(
        "production", PROD_ENV, "{'v': settings.SILENCED_SYSTEM_CHECKS}"
    )
    assert rc == 0, err
    assert json.loads(out)["v"] == ["security.W021"]
    source = (SETTINGS_DIR / "production.py").read_text()
    assert "security.W021 —" in source, "W021 is silenced without a written justification"


def test_hardening_does_not_leak_into_development(load_settings):
    """Secure cookies over plain HTTP would break local login."""
    rc, out, err = load_settings(
        "development", {}, "{'v': getattr(settings, 'SESSION_COOKIE_SECURE', False)}"
    )
    assert rc == 0, err
    assert json.loads(out)["v"] is False


def test_compose_pins_the_settings_layer_per_service():
    """A shared .env must not be able to set the layer for both services.

    Regression: docker-compose.yml used ${DJANGO_SETTINGS_MODULE:-...} for both
    services, so a .env written for local work silently made the
    production-like profile run DEVELOPMENT settings — DEBUG on, no HSTS, no
    secure cookies. The profile looked healthy and proved nothing.
    """
    compose = (REPO_ROOT / "docker-compose.yml").read_text()
    assert 'DJANGO_SETTINGS_MODULE: "config.settings.development"' in compose
    assert 'DJANGO_SETTINGS_MODULE: "config.settings.production"' in compose
    assert "${DJANGO_SETTINGS_MODULE" not in compose, (
        "the settings layer must be pinned per service, not interpolated"
    )


def test_env_example_does_not_set_layer_specific_values():
    """Values that differ per layer must ship commented out.

    A .env is read by both Compose profiles. An uncommented DJANGO_DEBUG there
    stops production from starting; an uncommented DJANGO_SETTINGS_MODULE makes
    production run development settings.
    """
    example = (REPO_ROOT / ".env.example").read_text()
    for var in ("DJANGO_DEBUG", "DJANGO_SETTINGS_MODULE"):
        uncommented = [
            line for line in example.splitlines() if line.strip().startswith(f"{var}=")
        ]
        assert not uncommented, f"{var} must be commented out in .env.example: {uncommented}"
