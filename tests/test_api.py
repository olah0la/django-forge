"""The API instance and its versioned mount.

These guard the wiring, not django-ninja: that the API answers under the
version prefix, that the prefix is real, that the OpenAPI document carries the
configured metadata rather than Ninja's defaults, and that the documentation
page is on in development and off in production.

The versioning assertions matter more than they look. A prefix that quietly
stops being required, or a namespace that follows the document version, are
both invisible until a client depends on the old behaviour — at which point the
fix is a coordinated migration rather than a commit.

The second half (M5-02) guards the router composition pattern: endpoints defined
in apps, mounted in one table, with the conventions that keep the OpenAPI
document navigable.
"""

import json

import pytest
from django.conf import settings
from django.test import Client, override_settings
from ninja import NinjaAPI
from ninja.errors import ConfigError

from apps.core.api import router as core_router
from config.api import ROUTERS, V1_NAMESPACE, V1_PREFIX, api, build_api
from tests.testapp.api import router as things_router

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
        built = build_api()
        assert getattr(built, setting_name.removeprefix("API_").lower()) == overridden


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
        built = build_api()
        assert built.docs_url is None
        assert built.openapi_url is None


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


# ---------------------------------------------------------------------------
# Router composition (M5-02)
# ---------------------------------------------------------------------------
# These assert the *shape* of the API rather than a response, because that is
# what the issue is about: endpoints living in apps, mounted in one place. Every
# one of them keeps passing if a response body changes, and fails the moment the
# pattern is abandoned — which is the only way it gets abandoned, quietly, under
# time pressure.
def _schema(built: NinjaAPI) -> dict:
    """The OpenAPI document an instance would serve, without touching .urls.

    `.urls` registers the namespace and freezes the router list, so reading it
    on an instance built for inspection raises ConfigError — see build_api().

    The prefix keeps its TRAILING SLASH, matching the mount in config/urls.py.
    Without it Ninja concatenates rather than joins, and a router mounted at
    `things` reports `/api/v1things/` — a wrong path in a passing test.
    """
    return built.get_openapi_schema(path_prefix=f"{BASE}/")


def _operations(built: NinjaAPI):
    """Every registered operation, with the router it came from.

    Reaches into Ninja's internals (`_routers`, `path_operations`), which no
    public API exposes. Worth the coupling: the alternative is asserting on
    generated URLs, which cannot see *where the handler was defined* — and that
    is precisely what criterion 2 is about.
    """
    for _prefix, router in built._routers:
        for path_view in router.path_operations.values():
            yield from path_view.operations


def test_the_instance_holds_no_endpoints_of_its_own():
    """M5-02 criterion 2, asserted where the failure would actually land.

    `@api.get(...)` on the instance registers on its default router. Empty is
    the whole point: this file must stay a mounting table, or it grows without
    bound and every feature branch conflicts in it.
    """
    assert api.default_router.path_operations == {}


def test_every_endpoint_is_defined_in_an_app():
    """The same rule from the other direction.

    An endpoint could still be defined in `config/` and attached to a router
    declared there, which the previous test would not catch. Handlers belong to
    apps; `config/` only wires them together.
    """
    modules = {operation.view_func.__module__ for operation in _operations(api)}

    assert modules, "no operations registered at all — the mount table is empty"
    assert all(module.startswith("apps.") for module in modules), modules


def test_every_operation_is_tagged_by_its_router():
    """M5-02 criterion 3.

    The tag is set once on the `Router`, not per endpoint, so an endpoint cannot
    be added untagged by forgetting a decorator argument. Untagged operations
    land in Swagger UI's "default" group, which is where an API stops being
    navigable.
    """
    for path, methods in _schema(api)["paths"].items():
        for method, operation in methods.items():
            assert operation.get("tags"), f"{method.upper()} {path} has no tag"


def test_operation_ids_are_unique():
    """A collision is only *printed* by Ninja, never raised.

    Two endpoints sharing an operationId produce a document that generated
    clients cannot represent — one method silently overwrites the other — and
    the only warning is a line in the server log nobody is reading.
    """
    ids = [
        operation["operationId"]
        for methods in _schema(api)["paths"].values()
        for operation in methods.values()
    ]

    assert len(ids) == len(set(ids)), ids


def test_operation_ids_follow_the_module_path():
    """Documented because it is a client-visible consequence of moving a file.

    Ninja derives operationId as `module_name`, so moving a router from one
    module to another renames every generated client method. This assertion
    exists to make that concrete: `ping` moved from `config/api.py` to
    `apps/core/api.py` in M5-02, and its id changed from `config_api_ping`.
    Endpoints with published clients pin `operation_id=` explicitly.
    """
    ping = _schema(api)["paths"][f"{BASE}/ping"]["get"]

    assert ping["operationId"] == "apps_core_api_ping"
    assert ping["tags"] == ["meta"]


def test_core_is_the_only_router_mounted_at_the_root():
    """The exception is deliberate, and deliberately narrow.

    Meta endpoints answer for the API itself rather than for a collection, so
    core mounts at the root: /api/v1/ping, not /api/v1/core/ping. A feature app
    doing the same would put its resources in the API's root namespace, where
    the next app's resources collide with them.
    """
    rooted = [prefix for prefix, _router in ROUTERS if prefix == ""]

    assert len(rooted) == 1
    assert ROUTERS[0] == ("", core_router)


def test_a_mounted_router_serves_under_its_prefix():
    """M5-02 criteria 1 and 3, with a router shaped like a feature app's.

    A throwaway instance, because the shipped API has only core on it — and core
    is the one router that does NOT take a prefix. Nothing is mounted on the
    real `api`; see tests/testapp/api.py.
    """
    built = build_api()
    built.add_router("things", things_router)

    paths = _schema(built)["paths"]

    assert f"{BASE}/things/" in paths
    assert f"{BASE}/ping" in paths, "mounting a second router displaced the first"
    assert paths[f"{BASE}/things/"]["get"]["tags"] == ["things"]


def test_mounting_the_same_router_twice_is_refused():
    """The failure the issue's implementation notes name.

    It happens for real when a router is mounted from two places — an app
    registering itself as well as being registered centrally. Ninja catches it,
    which is the argument for keeping every mount in one visible table rather
    than scattered across AppConfig.ready() hooks.
    """
    built = build_api()

    with pytest.raises(ConfigError, match="already mounted"):
        built.add_router("core-again", core_router)


def test_routers_cannot_be_mounted_after_the_urls_are_built():
    """Why ROUTERS is a module-level table and not a runtime registry.

    Ninja freezes the router list the first time `.urls` is read — which happens
    when Django loads the URLconf. Anything that mounts a router later (an
    AppConfig.ready() hook, a lazy import, a plugin) fails at startup, and the
    message names URL generation rather than the mount that was too late.
    """
    built = build_api()
    built.urls_namespace = "api-v1-throwaway"  # never share V1_NAMESPACE
    _ = built.urls

    with pytest.raises(ConfigError, match="after URLs have been generated"):
        built.add_router("things", things_router)
