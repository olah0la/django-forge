"""The API instance and its versioned mount.

These guard the wiring, not django-ninja: that the API answers under the
version prefix, that the prefix is real, that the OpenAPI document carries the
configured metadata rather than Ninja's defaults, and that the documentation
page is on in development and off in production.

The versioning assertions matter more than they look. A prefix that quietly
stops being required, or a namespace that follows the document version, are
both invisible until a client depends on the old behaviour — at which point the
fix is a coordinated migration rather than a commit.
"""

import json

import pytest
from django.conf import settings
from django.test import Client, override_settings

from config.api import V1_NAMESPACE, V1_PREFIX, build_api

BASE = f"/api/{V1_PREFIX}"


@pytest.fixture
def client():
    return Client()


# ---------------------------------------------------------------------------
# The versioned mount
# ---------------------------------------------------------------------------
def test_endpoint_responds_through_the_versioned_path(client):
    """M5-01 criterion 3."""
    response = client.get(f"{BASE}/ping")

    assert response.status_code == 200
    assert response.json() == {"pong": True, "version": settings.API_VERSION}


def test_the_prefix_is_load_bearing(client):
    """The same endpoint must NOT answer unprefixed.

    Asserting only that the prefixed path works would keep passing if the API
    were also mounted at the root — which is the state this issue exists to
    prevent, because clients would then integrate against an unversioned URL.
    """
    assert client.get("/ping").status_code == 404


def test_namespace_does_not_follow_the_document_version():
    """Ninja defaults urls_namespace to "api-" + version; this one is pinned.

    Left at the default, bumping API_VERSION from 1.0.0 to 1.1.0 — an ordinary
    additive release — would rename the URL namespace and break every reverse()
    against it. The namespace identifies the instance, so it tracks the URL
    prefix instead.
    """
    assert V1_NAMESPACE == f"api-{V1_PREFIX}"

    with override_settings(API_VERSION="9.9.9"):
        assert build_api().urls_namespace == V1_NAMESPACE


# ---------------------------------------------------------------------------
# Configurable metadata (criterion 2)
# ---------------------------------------------------------------------------
def test_openapi_document_carries_the_configured_metadata(client):
    """Not Ninja's "NinjaAPI" / "1.0.0" defaults.

    This is what catches the wiring being dropped: an instance constructed with
    no arguments still serves a perfectly valid schema, just one describing a
    project nobody recognises.
    """
    response = client.get(f"{BASE}/openapi.json")
    assert response.status_code == 200

    info = json.loads(response.content)["info"]
    assert info["title"] == settings.API_TITLE
    assert info["version"] == settings.API_VERSION
    assert info["description"] == settings.API_DESCRIPTION
    assert info["title"] != "NinjaAPI", "title fell back to Ninja's default"


@pytest.mark.parametrize(
    "setting_name",
    ["API_TITLE", "API_VERSION", "API_DESCRIPTION"],
)
def test_metadata_is_configurable_rather_than_hard_coded(setting_name):
    overridden = "configured-elsewhere"
    with override_settings(**{setting_name: overridden}):
        api = build_api()
        assert getattr(api, setting_name.removeprefix("API_").lower()) == overridden


# ---------------------------------------------------------------------------
# Interactive documentation (criterion 5)
# ---------------------------------------------------------------------------
def test_docs_are_reachable_when_enabled(client):
    assert settings.API_DOCS_ENABLED is True
    assert client.get(f"{BASE}/docs").status_code == 200


def test_docs_render_from_bundled_assets_not_a_cdn(client):
    """"ninja" is in INSTALLED_APPS for exactly this reason.

    Without that entry Ninja falls back to a template that loads swagger-ui
    from cdn.jsdelivr.net: the page then breaks offline, and every developer
    opening it makes a request to a third party. The API keeps working either
    way, which is what makes the INSTALLED_APPS entry look removable.
    """
    assert "ninja" in settings.INSTALLED_APPS

    html = client.get(f"{BASE}/docs").content.decode()
    assert "jsdelivr" not in html
    assert "/static/ninja/swagger-ui-bundle.js" in html


def test_disabling_docs_removes_the_schema_too():
    """Both routes, not just the browsable one.

    Disabling `docs_url` alone looks finished — the page disappears — while
    openapi.json keeps serving. Measured against the production layer during
    M5-01: /api/v1/docs returned 404 and /api/v1/openapi.json returned 200.
    The JSON is the more useful artefact to someone enumerating an API, so
    hiding only the page achieves nothing. These stay coupled.
    """
    with override_settings(API_DOCS_ENABLED=False):
        api = build_api()
        assert api.docs_url is None
        assert api.openapi_url is None


def test_production_serves_no_docs_by_default(load_settings):
    """The setting that matters, checked against the layer that ships.

    A fresh interpreter, because settings execute once at import — the
    in-process layer is the test one, and asserting on it would prove nothing
    about production. Same idiom as tests/test_settings.py.
    """
    code, stdout, stderr = load_settings(
        "production",
        {
            "DJANGO_SECRET_KEY": "x" * 50,
            "DJANGO_ALLOWED_HOSTS": "example.com",
        },
        "{'docs': settings.API_DOCS_ENABLED}",
    )

    assert code == 0, stderr
    assert json.loads(stdout)["docs"] is False


def test_production_docs_can_be_turned_on_deliberately(load_settings):
    """Off by default, but not forbidden — public API docs are a real choice.

    This is the difference from SEED_ENABLED, which has no environment switch
    at all because seeding production is never correct.
    """
    code, stdout, stderr = load_settings(
        "production",
        {
            "DJANGO_SECRET_KEY": "x" * 50,
            "DJANGO_ALLOWED_HOSTS": "example.com",
            "DJANGO_API_DOCS_ENABLED": "true",
        },
        "{'docs': settings.API_DOCS_ENABLED}",
    )

    assert code == 0, stderr
    assert json.loads(stdout)["docs"] is True
