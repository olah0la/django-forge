"""The liveness and readiness probes (M6-01).

The distinction is the whole issue, so the tests are organised around it: what
liveness must NOT do, what readiness must catch, and what neither may disclose.

Three of these guard properties that fail silently and expensively:

* liveness issuing a query would make it fail during a database outage, and the
  platform would restart every replica of a healthy service;
* readiness disclosing why it failed puts infrastructure detail in an
  unauthenticated response body;
* the probe paths being unreachable in the production layer — through the SSL
  redirect or Host validation — makes every container report unhealthy for a
  reason that looks nothing like its cause.

None of them break a test that only asserts "200 OK".
"""

import json
import logging
import re
from pathlib import Path
from unittest import mock

import pytest
from django.conf import settings
from django.db import OperationalError
from django.test import Client
from django.urls import reverse

from apps.core import health
from config.logging import SuppressHealthCheckAccessLogs

REPO_ROOT = Path(__file__).resolve().parent.parent

LIVENESS, READINESS = settings.HEALTH_CHECK_PATHS


@pytest.fixture
def client():
    return Client()


@pytest.fixture
def database_down():
    """Make every readiness database check fail, the way an outage would.

    Patched at the cursor, not at the view: the point is that whatever the check
    actually does against the database, a failure there produces a 503. The
    manual verification that closes acceptance criterion 3 is `docker compose
    stop db` — this is the version that runs in CI on every commit.
    """
    with mock.patch(
        "apps.core.health.connections",
        **{
            "__getitem__.return_value.cursor.side_effect": OperationalError(
                'connection to server at "db" (172.18.0.2), port 5432 failed'
            )
        },
    ) as patched:
        yield patched


# ---------------------------------------------------------------------------
# Liveness: "should the platform restart this process?"
# ---------------------------------------------------------------------------
def test_liveness_responds(client):
    response = client.get(LIVENESS)

    assert response.status_code == 200
    assert response.json() == {"status": "alive"}


@pytest.mark.django_db
def test_liveness_touches_no_database(client, django_assert_num_queries):
    """Acceptance criterion 1, and the one that fails silently.

    A liveness probe that reaches a dependency turns a database outage into a
    restart loop across every replica — strictly worse than the outage alone.
    Nothing in the view queries today; the risk is a middleware added later that
    does, which this catches on the commit that adds it.
    """
    with django_assert_num_queries(0):
        assert client.get(LIVENESS).status_code == 200


def test_liveness_stays_up_while_the_database_is_down(client, database_down):
    """The distinction, stated as a test.

    If this ever returns non-200 during an outage, the platform kills healthy
    processes at exactly the moment the service is least able to absorb it.
    """
    assert client.get(LIVENESS).status_code == 200
    assert client.get(READINESS).status_code == 503


# ---------------------------------------------------------------------------
# Readiness: "should this process receive traffic?"
# ---------------------------------------------------------------------------
@pytest.mark.django_db
def test_readiness_responds_when_dependencies_answer(client):
    response = client.get(READINESS)

    assert response.status_code == 200
    assert response.json() == {"status": "ready"}


def test_readiness_fails_when_the_database_is_unavailable(client, database_down):
    """Acceptance criterion 3, in the form that runs on every commit.

    503, not 500: this is a temporary inability to serve, which is what load
    balancers act on.
    """
    response = client.get(READINESS)

    assert response.status_code == 503
    assert response.json() == {"status": "not ready"}


@pytest.mark.django_db
def test_readiness_runs_every_check(client):
    """A second failing check must fail the probe, not be skipped after the first.

    Guards the loop rather than the single check that exists today: a downstream
    project adding one to READINESS_CHECKS gets the same behaviour.
    """
    with mock.patch.object(
        health,
        "READINESS_CHECKS",
        (("database", lambda: True), ("something-else", lambda: False)),
    ):
        assert client.get(READINESS).status_code == 503


def test_readiness_discloses_nothing_about_the_failure(client, database_down, caplog):
    """Acceptance criterion 4.

    The probe is unauthenticated by necessity, so its body goes to anything that
    can reach the port. "connection to server at db (172.18.0.2), port 5432
    failed" is free reconnaissance — it belongs in the log, and the test asserts
    it is in exactly one of the two places.
    """
    with caplog.at_level(logging.WARNING, logger="apps.core.health"):
        response = client.get(READINESS)

    body = response.content.decode()
    for leak in ("db", "5432", "172.18.0.2", "OperationalError", "database"):
        assert leak not in body, f"the readiness body disclosed {leak!r}"
    assert json.loads(body) == {"status": "not ready"}

    # ...but an operator still has to be able to find out which check failed.
    assert "database" in caplog.text


# ---------------------------------------------------------------------------
# What both endpoints must and must not accept
# ---------------------------------------------------------------------------
@pytest.mark.django_db
@pytest.mark.parametrize("path", [LIVENESS, READINESS])
def test_probes_need_no_authentication(client, path):
    """Acceptance criterion 4. A probe cannot hold credentials."""
    assert client.get(path).status_code in (200, 503)


@pytest.mark.django_db
@pytest.mark.parametrize("path", [LIVENESS, READINESS])
def test_probes_reject_unsafe_methods(client, path):
    assert client.post(path).status_code == 405


@pytest.mark.django_db
@pytest.mark.parametrize("path", [LIVENESS, READINESS])
def test_probe_responses_are_not_cacheable(client, path):
    """A cached probe response is a lie told at the worst possible moment."""
    assert "no-store" in client.get(path).headers["Cache-Control"]


@pytest.mark.parametrize("name, path", [("liveness", LIVENESS), ("readiness", READINESS)])
def test_probes_resolve_at_the_documented_paths(name, path):
    assert reverse(name) == path


@pytest.mark.parametrize("path", [LIVENESS, READINESS])
def test_probes_are_not_under_the_api_version_prefix(client, path):
    """The paths are an INFRASTRUCTURE contract, not an API one.

    Serving them under /api/v1/ as well would let a deployment integrate against
    the versioned path, and a future v2 would then move a URL that lives in
    Dockerfiles and orchestrator manifests — a breaking change for a reason
    unrelated to the API changing at all.
    """
    assert client.get(f"/api/v1{path}").status_code == 404


def test_probe_paths_are_absent_from_the_openapi_document(client):
    """They are not part of the published API surface, so they are not described.

    Not cosmetic: openapi.json is what a consumer generates a client from, and a
    probe in it invites application traffic at an endpoint meant for the
    platform.
    """
    document = client.get("/api/v1/openapi.json").json()
    for path in document["paths"]:
        assert "healthz" not in path and "readyz" not in path


# ---------------------------------------------------------------------------
# Excluded from request logging (acceptance criterion 5)
# ---------------------------------------------------------------------------
def _uvicorn_access_record(path: str) -> logging.LogRecord:
    """A record shaped exactly like uvicorn's access log call.

    Pinned here because the filter reads args[2], which is coupling to another
    project's logging call. If uvicorn changes the shape, this test is what says
    so — rather than the probes quietly reappearing in production logs.
    """
    return logging.LogRecord(
        name="uvicorn.access",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg='%s - "%s %s HTTP/%s" %d',
        args=("127.0.0.1:53712", "GET", path, "1.1", 200),
        exc_info=None,
    )


@pytest.mark.parametrize("path", [LIVENESS, READINESS, f"{READINESS}?verbose=1"])
def test_probe_access_lines_are_dropped(path):
    assert SuppressHealthCheckAccessLogs().filter(_uvicorn_access_record(path)) is False


@pytest.mark.parametrize("path", ["/api/v1/ping", "/admin/", "/healthz-not-really"])
def test_real_request_lines_are_kept(path):
    assert SuppressHealthCheckAccessLogs().filter(_uvicorn_access_record(path)) is True


def _django_request_record(path: str, *, exc_info=None) -> logging.LogRecord:
    """A record shaped like django.request's, which logs every 4xx and 5xx.

    The non-obvious half of criterion 5: during a database outage, every
    readiness probe is a 503 and Django logs "Service Unavailable: /readyz" at
    ERROR, from every replica, for the duration of the outage.
    `django.utils.log.log_response` attaches the request through `extra`.
    """
    record = logging.LogRecord(
        name="django.request",
        level=logging.ERROR,
        pathname=__file__,
        lineno=1,
        msg="%s: %s",
        args=("Service Unavailable", path),
        exc_info=exc_info,
    )
    record.request = mock.Mock(path=path)
    record.status_code = 503
    return record


@pytest.mark.parametrize("path", [LIVENESS, READINESS])
def test_probe_error_lines_are_dropped(path):
    assert SuppressHealthCheckAccessLogs().filter(_django_request_record(path)) is False


def test_real_error_lines_are_kept():
    assert SuppressHealthCheckAccessLogs().filter(_django_request_record("/api/v1/ping")) is True


def test_a_crash_inside_a_probe_view_still_reaches_the_log():
    """The exemption that makes the django.request filter safe.

    Django logs an unhandled exception at the same level, through the same
    logger, as an ordinary 5xx. Without this carve-out, a genuine crash inside
    the readiness view — the one moment the traceback is indispensable — would be
    dropped along with the noise, and the endpoint would look merely "not ready".
    """
    record = _django_request_record(READINESS, exc_info=(ValueError, ValueError("boom"), None))

    assert SuppressHealthCheckAccessLogs().filter(record) is True


@pytest.mark.parametrize(
    "args",
    [None, (), ("only", "three", "args"), (1, 2, 3, 4, 5)],
    ids=["none", "empty", "wrong-length", "non-string-path"],
)
def test_the_filter_fails_open_on_an_unrecognised_record(args):
    """Anything it cannot positively identify as a probe is KEPT.

    The filter couples to uvicorn's log call. When that coupling breaks, the
    acceptable failure is a noisier log — never a filter that silently swallows
    real request lines and leaves an operator debugging against a log with holes
    in it.
    """
    record = _uvicorn_access_record("/healthz")
    record.args = args

    assert SuppressHealthCheckAccessLogs().filter(record) is True


def test_the_logging_config_keeps_a_handler_on_the_access_logger():
    """dictConfig CLEARS a named logger's handlers.

    An entry declaring only `filters` would therefore pass criterion 5 by
    deleting access logging entirely, which is not what it asks for. This is the
    line that makes the difference, and it looks redundant.
    """
    access = settings.LOGGING["loggers"]["uvicorn.access"]
    assert access["handlers"], "uvicorn.access must keep a handler"
    assert access["propagate"] is False, "otherwise every access line is logged twice"
    assert settings.LOGGING["disable_existing_loggers"] is False

    handler = settings.LOGGING["handlers"][access["handlers"][0]]
    assert "suppress_health_checks" in handler["filters"]


def test_the_logging_config_leaves_django_request_its_inherited_handlers():
    """The mirror image: here declaring `handlers` is what would break it.

    django.request has no handlers of its own and propagates to Django's logger.
    The filter therefore sits on the LOGGER — returning False there stops
    propagation as well — and adding a `handlers` key would clear what it
    inherits.
    """
    request_logger = settings.LOGGING["loggers"]["django.request"]

    assert "suppress_health_checks" in request_logger["filters"]
    assert "handlers" not in request_logger, (
        "declaring handlers here clears the ones django.request inherits"
    )


# ---------------------------------------------------------------------------
# The production layer: the two traps that make probes fail there
# ---------------------------------------------------------------------------
@pytest.fixture
def production_settings(load_settings):
    def _load(expression: str, **env: str):
        rc, out, err = load_settings(
            "production",
            {
                "DJANGO_SECRET_KEY": "x" * 50,
                "DJANGO_ALLOWED_HOSTS": "example.com",
                **env,
            },
            expression,
        )
        assert rc == 0, err
        return json.loads(out)

    return _load


def test_production_exempts_the_probes_from_the_ssl_redirect(production_settings):
    """Without this, every probe gets a 301 to https:// and never a 200.

    The container reports permanently unhealthy, and the symptom — a healthy
    application that Docker says is broken — points nowhere near the cause.
    """
    result = production_settings(
        "{'exempt': settings.SECURE_REDIRECT_EXEMPT, 'redirect': settings.SECURE_SSL_REDIRECT}"
    )

    assert result["redirect"] is True, "the exemption is only load-bearing while this is on"
    for path in settings.HEALTH_CHECK_PATHS:
        assert any(re.match(pattern, path.lstrip("/")) for pattern in result["exempt"]), (
            f"{path} is not exempt from the SSL redirect"
        )


def test_production_exemption_does_not_match_anything_else(production_settings):
    """Anchored patterns, so `/healthz-admin` is not quietly served over plain HTTP."""
    exempt = production_settings("{'exempt': settings.SECURE_REDIRECT_EXEMPT}")["exempt"]

    for path in ("healthzz", "readyz/secret", "admin/healthz", "xhealthz"):
        assert not any(re.match(pattern, path) for pattern in exempt), (
            f"{path} must not be exempt from the SSL redirect"
        )


def test_production_allows_the_loopback_host_the_probe_connects_to(production_settings):
    """The probe runs inside the container and cannot know the public hostname.

    Without loopback in ALLOWED_HOSTS it gets a 400 DisallowedHost, and the
    container is unhealthy for every adopter who set DJANGO_ALLOWED_HOSTS
    correctly.
    """
    hosts = production_settings("{'hosts': settings.ALLOWED_HOSTS}")["hosts"]

    assert "example.com" in hosts, "the operator's host must survive"
    assert "127.0.0.1" in hosts and "localhost" in hosts


def test_production_does_not_widen_to_a_wildcard(production_settings):
    """Two loopback names is a different thing from ALLOWED_HOSTS = ["*"].

    Naming that here is the point: the fix above is the kind that gets
    "simplified" into a wildcard by someone in a hurry.
    """
    hosts = production_settings("{'hosts': settings.ALLOWED_HOSTS}")["hosts"]

    assert "*" not in hosts
    assert not any(h.startswith(".") for h in hosts)


def test_loopback_is_not_duplicated_when_the_operator_supplies_it(production_settings):
    hosts = production_settings(
        "{'hosts': settings.ALLOWED_HOSTS}",
        DJANGO_ALLOWED_HOSTS="example.com,localhost",
    )["hosts"]

    assert hosts.count("localhost") == 1


# ---------------------------------------------------------------------------
# The container health check
# ---------------------------------------------------------------------------
def test_dockerfile_healthcheck_requests_the_readiness_endpoint():
    """It proved `import django` until M6-01. That is a runtime check, not a
    health check: the interpreter is intact in a container that cannot serve."""
    dockerfile = (REPO_ROOT / "Dockerfile").read_text()
    healthcheck = dockerfile[dockerfile.index("\nHEALTHCHECK ") :].split("\n\n")[0]

    assert READINESS in healthcheck, "the container probe must request the readiness endpoint"
    assert "import django" not in healthcheck


def test_dockerfile_healthcheck_start_period_covers_the_database_wait():
    """docker-entrypoint.sh waits up to DB_WAIT_TIMEOUT for the database BEFORE
    the server starts, so a start period below that reports a normal cold boot
    as a failure."""
    dockerfile = (REPO_ROOT / "Dockerfile").read_text()
    entrypoint = (REPO_ROOT / "docker-entrypoint.sh").read_text()

    start_period = int(re.search(r"--start-period=(\d+)s", dockerfile).group(1))
    db_wait = int(re.search(r"DB_WAIT_TIMEOUT:-(\d+)\}", entrypoint).group(1))

    assert start_period > db_wait, (
        f"--start-period={start_period}s does not cover the {db_wait}s database wait"
    )
