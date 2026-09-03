"""Structured logging and request correlation (M6-04).

Three kinds of test, because the subject has three kinds of surface:

* the formatter, exercised directly on synthetic records — the only way to
  assert what it does with a record it must NOT publish;
* the middleware, exercised over real HTTP through `tests/testapp/urls.py`;
* the per-layer configuration, exercised by importing each settings layer in a
  fresh subprocess via the `load_settings` fixture, because settings execute
  once at import and the first import wins for the life of the run.
"""

import json
import logging
import sys

import pytest
from asgiref.sync import async_to_sync, iscoroutinefunction
from django.http import HttpRequest, HttpResponse
from django.test import AsyncClient, Client, override_settings

from apps.core.logging import (
    NO_REQUEST_ID,
    REDACTED,
    JSONFormatter,
    RequestIDFilter,
    redact_mapping,
    request_id_var,
)
from apps.core.middleware import VALID_REQUEST_ID, RequestIDMiddleware

BOOM = "/api/v1/logging/boom"
THINGS = "/api/v1/things/"

# The strings that must never appear. Distinctive enough that a match cannot be
# a coincidence, and unrelated to each other so a partial leak is still caught.
PASSWORD = "pw-a3f9c1e7-must-not-appear"
TOKEN = "tok-b8d2f4a6-must-not-appear"
USERNAME = "leak-canary"

SECRET_PAYLOAD = json.dumps({"username": USERNAME, "password": PASSWORD, "token": TOKEN})


@pytest.fixture
def failing_client() -> Client:
    """A client that returns the 500 instead of re-raising the exception.

    The default test client re-raises whatever the view raised, which is
    usually what you want and is useless here: the question is what the SERVER
    logged while turning that exception into a response.
    """
    return Client(raise_request_exception=False)


def make_record(**attributes) -> logging.LogRecord:
    """A record with the standard fields filled in and `attributes` attached."""
    record = logging.LogRecord(
        name="apps.test",
        level=logging.INFO,
        pathname=__file__,
        lineno=42,
        msg="hello %s",
        args=("world",),
        exc_info=None,
    )
    for key, value in attributes.items():
        setattr(record, key, value)
    return record


def format_json(record: logging.LogRecord) -> dict:
    return json.loads(JSONFormatter().format(record))


# ---------------------------------------------------------------------------
# The JSON formatter
# ---------------------------------------------------------------------------


def test_output_is_a_single_json_object():
    line = JSONFormatter().format(make_record())

    assert "\n" not in line
    assert json.loads(line)["message"] == "hello world"


def test_the_expected_fields_are_present():
    payload = format_json(make_record(request_id="abc123"))

    assert payload["level"] == "INFO"
    assert payload["logger"] == "apps.test"
    assert payload["request_id"] == "abc123"
    assert payload["line"] == 42


def test_the_timestamp_is_utc_iso_8601_with_milliseconds():
    timestamp = format_json(make_record())["timestamp"]

    # e.g. 2026-09-02T13:30:52.602Z — sortable as a plain string and
    # unambiguous, neither of which `logging`'s local-time default manages.
    assert timestamp.endswith("Z")
    assert timestamp[10] == "T"
    assert len(timestamp.split(".")[1]) == 4  # three digits, then the Z


def test_a_traceback_stays_on_one_line():
    try:
        raise ValueError("boom")
    except ValueError:
        record = make_record(exc_info=sys.exc_info())

    line = JSONFormatter().format(record)

    # The property the whole format rests on: one record is one line, however
    # many lines the traceback had. json.dumps escapes them.
    assert "\n" not in line
    assert "ValueError: boom" in json.loads(line)["exception"]


def test_an_unserialisable_value_does_not_lose_the_line():
    class Opaque:
        def __repr__(self) -> str:
            return "<opaque>"

    payload = format_json(make_record(status=Opaque()))

    # `default=str` rather than an exception. A formatter that raises produces
    # no line at all, and the surprising value is the one worth having.
    assert payload["status"] == "<opaque>"


def test_the_request_attribute_is_never_published():
    """The reason the formatter uses an allow-list rather than "every extra".

    `django.request` attaches the live HttpRequest to the records it emits for
    a 4xx or 5xx. Its repr contains the full path INCLUDING THE QUERY STRING,
    so a formatter that serialises unknown attributes writes `?token=…` into
    the log.
    """

    class FakeRequest:
        def __repr__(self) -> str:
            return f"<ASGIRequest: POST '/x?token={TOKEN}'>"

    line = JSONFormatter().format(make_record(request=FakeRequest()))

    assert TOKEN not in line
    assert "request" not in json.loads(line)


def test_an_unknown_extra_is_dropped():
    assert "surprise" not in format_json(make_record(surprise="value"))


def test_a_record_that_missed_the_filter_still_formats():
    # pytest's caplog attaches its own handler, so records do reach formatters
    # without passing RequestIDFilter. A KeyError here would lose the line.
    assert format_json(make_record())["request_id"] == NO_REQUEST_ID


# ---------------------------------------------------------------------------
# The correlation filter
# ---------------------------------------------------------------------------


def test_the_filter_attaches_the_current_identifier():
    token = request_id_var.set("from-the-context")
    try:
        record = make_record()
        RequestIDFilter().filter(record)
        assert record.request_id == "from-the-context"
    finally:
        request_id_var.reset(token)


def test_the_filter_keeps_every_record():
    # A filter that returns False DROPS the line. This one only annotates.
    assert RequestIDFilter().filter(make_record()) is True


def test_outside_a_request_the_identifier_is_a_placeholder():
    record = make_record()
    RequestIDFilter().filter(record)

    assert record.request_id == NO_REQUEST_ID


# ---------------------------------------------------------------------------
# Redaction helpers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "key",
    ["password", "user_password", "TOKEN", "X-Api-Key", "Authorization", "DATABASE_URL"],
)
def test_sensitive_keys_are_masked(key):
    assert redact_mapping({key: "secret-value"})[key] == REDACTED


def test_ordinary_keys_are_left_alone():
    assert redact_mapping({"username": "ada"}) == {"username": "ada"}


# ---------------------------------------------------------------------------
# The inbound identifier
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("value", ["abc123", "a" * 128, "trace-1.2_3", "0199ab-cd"])
def test_usable_inbound_identifiers(value):
    assert VALID_REQUEST_ID.match(value)


@pytest.mark.parametrize(
    ("value", "why"),
    [
        ("", "empty"),
        ("a" * 129, "over the length cap"),
        ("with space", "whitespace"),
        ("trailing\n", "a trailing newline — `$` would have accepted this one"),
        ("two\nlines", "an embedded newline is a forged second log line"),
        ("../etc/passwd", "path characters"),
        ("<script>", "markup"),
    ],
)
def test_unusable_inbound_identifiers(value, why):
    assert not VALID_REQUEST_ID.match(value), why


# ---------------------------------------------------------------------------
# The middleware, over real HTTP
# ---------------------------------------------------------------------------


@override_settings(ROOT_URLCONF="tests.testapp.urls")
@pytest.mark.django_db
def test_the_response_carries_an_identifier(client):
    response = client.get(THINGS)

    assert VALID_REQUEST_ID.match(response["X-Request-ID"])


@override_settings(ROOT_URLCONF="tests.testapp.urls")
@pytest.mark.django_db
def test_an_inbound_identifier_is_echoed_back(client):
    response = client.get(THINGS, headers={"x-request-id": "from-the-caller"})

    # This is what lets one request be followed across two services.
    assert response["X-Request-ID"] == "from-the-caller"


@override_settings(ROOT_URLCONF="tests.testapp.urls")
@pytest.mark.django_db
def test_a_malformed_inbound_identifier_is_replaced_not_refused(client):
    response = client.get(THINGS, headers={"x-request-id": "a" * 500})

    assert response.status_code == 200, "a bad header must not fail the request"
    assert response["X-Request-ID"] != "a" * 500
    assert VALID_REQUEST_ID.match(response["X-Request-ID"])


@override_settings(ROOT_URLCONF="tests.testapp.urls")
@pytest.mark.django_db
def test_each_request_gets_its_own_identifier(client):
    first = client.get(THINGS)["X-Request-ID"]
    second = client.get(THINGS)["X-Request-ID"]

    assert first != second


@override_settings(ROOT_URLCONF="tests.testapp.urls")
@pytest.mark.django_db
def test_the_request_is_logged_with_its_fields(client, caplog):
    with caplog.at_level(logging.INFO, logger="apps.request"):
        client.get(THINGS)

    (record,) = [r for r in caplog.records if r.name == "apps.request"]
    assert record.method == "GET"
    assert record.path == THINGS
    assert record.status == 200
    assert record.duration_ms >= 0


@override_settings(ROOT_URLCONF="tests.testapp.urls")
@pytest.mark.django_db
def test_the_query_string_is_not_logged(client, caplog):
    with caplog.at_level(logging.INFO, logger="apps.request"):
        client.get(THINGS, {"token": TOKEN})

    (record,) = [r for r in caplog.records if r.name == "apps.request"]
    assert record.path == THINGS
    assert TOKEN not in JSONFormatter().format(record)


@override_settings(ROOT_URLCONF="tests.testapp.urls")
@pytest.mark.django_db
def test_every_line_of_one_request_carries_the_same_identifier(failing_client, caplog):
    """The whole point: one request, several lines, one value joining them.

    The filter is added to caplog's handler because that is where the real
    handler has it — records are annotated as they are captured, while the
    request's context is still current.
    """
    caplog.handler.addFilter(RequestIDFilter())

    with caplog.at_level(logging.INFO):
        response = failing_client.post(BOOM, data=SECRET_PAYLOAD, content_type="application/json")

    assert response.status_code == 500

    loggers = {record.name for record in caplog.records}
    assert "apps.testapp" in loggers, "the view's own line"
    assert "apps.request" in loggers, "the request line"
    assert "django.request" in loggers, "Django's 500"

    identifiers = {record.request_id for record in caplog.records}
    assert identifiers == {response["X-Request-ID"]}


@override_settings(ROOT_URLCONF="tests.testapp.urls")
@pytest.mark.django_db
def test_djangos_own_4xx_line_is_correlated(client, caplog):
    """The gap the context variable alone does not close.

    Django logs 4xx and 5xx responses from `BaseHandler.get_response`, AFTER
    the middleware chain has returned — outside the middleware that sets the
    identifier. A 500 is correlated anyway, because an exception is logged from
    inside the chain; a plain 404 is not, which is what made this easy to miss.
    """
    caplog.handler.addFilter(RequestIDFilter())

    with caplog.at_level(logging.INFO):
        response = client.get("/no-such-path")

    assert response.status_code == 404

    (django_line,) = [r for r in caplog.records if r.name == "django.request"]
    assert django_line.request_id == response["X-Request-ID"]
    assert django_line.request_id != NO_REQUEST_ID


@override_settings(ROOT_URLCONF="tests.testapp.urls")
@pytest.mark.django_db
def test_the_identifier_does_not_leak_between_requests(client, caplog):
    caplog.handler.addFilter(RequestIDFilter())

    with caplog.at_level(logging.INFO):
        client.get(THINGS)
        # Between requests the context is reset. Under ASGI the same task
        # serves later requests, so a leaked identifier would not be lost — it
        # would be attached to someone else's request.
        logging.getLogger("apps.test").info("between requests")

    (between,) = [r for r in caplog.records if r.getMessage() == "between requests"]
    assert between.request_id == NO_REQUEST_ID


@override_settings(ROOT_URLCONF="tests.testapp.urls", REQUEST_LOG_EXCLUDED_PATHS=[THINGS])
@pytest.mark.django_db
def test_an_excluded_path_is_not_logged_but_is_still_identified(client, caplog):
    with caplog.at_level(logging.INFO, logger="apps.request"):
        response = client.get(THINGS)

    assert not [r for r in caplog.records if r.name == "apps.request"]
    # The header still goes out: a caller reporting a failed probe needs
    # something to quote, even when the probe is too noisy to log.
    assert VALID_REQUEST_ID.match(response["X-Request-ID"])


# ---------------------------------------------------------------------------
# The async path
# ---------------------------------------------------------------------------
# ASGI is the served interface (M3-04), so `__acall__` — not `__call__` — is
# the code that actually runs in production. Everything above drives the sync
# client, which never reaches it.
#
# `async_to_sync` rather than an async test, because this project has no async
# test plugin and does not need one for a single case.


def test_an_async_stack_produces_an_awaitable_middleware():
    """`markcoroutinefunction` is what tells Django not to wrap this instance
    in a threadpool. Forget it and everything still works, one hidden thread
    hop per request slower, with nothing failing to say so.
    """

    async def get_response(request: HttpRequest) -> HttpResponse:
        return HttpResponse()

    middleware = RequestIDMiddleware(get_response)

    assert middleware.async_mode is True
    assert iscoroutinefunction(middleware)


def test_a_sync_stack_produces_a_plain_middleware():
    middleware = RequestIDMiddleware(lambda request: HttpResponse())

    assert middleware.async_mode is False
    assert not iscoroutinefunction(middleware)


@override_settings(ROOT_URLCONF="tests.testapp.urls")
def test_the_middleware_works_on_the_asgi_path(caplog):
    caplog.handler.addFilter(RequestIDFilter())
    client = AsyncClient(raise_request_exception=False)

    with caplog.at_level(logging.INFO):
        response = async_to_sync(client.post)(
            BOOM, data=SECRET_PAYLOAD, content_type="application/json"
        )

    assert response.status_code == 500
    assert VALID_REQUEST_ID.match(response["X-Request-ID"])

    request_lines = [r for r in caplog.records if r.name == "apps.request"]
    assert request_lines, "the request was not logged on the async path"
    assert {r.request_id for r in request_lines} == {response["X-Request-ID"]}


@override_settings(ROOT_URLCONF="tests.testapp.urls")
def test_secrets_do_not_reach_the_log_on_the_asgi_path(caplog):
    with caplog.at_level(logging.INFO):
        async_to_sync(AsyncClient(raise_request_exception=False).post)(
            f"{BOOM}?token={TOKEN}", data=SECRET_PAYLOAD, content_type="application/json"
        )

    stream = "\n".join(JSONFormatter().format(record) for record in caplog.records)

    assert USERNAME in stream, "nothing was logged; the absences below are vacuous"
    assert PASSWORD not in stream
    assert TOKEN not in stream


# ---------------------------------------------------------------------------
# THE ACCEPTANCE CRITERION: secrets never reach the log
# ---------------------------------------------------------------------------


@override_settings(ROOT_URLCONF="tests.testapp.urls")
@pytest.mark.django_db
def test_a_password_sent_to_a_failing_endpoint_never_reaches_the_log(failing_client, caplog):
    """Trigger an error on an endpoint receiving sensitive input, then read the
    log exactly as production would write it.

    Everything is rendered through JSONFormatter rather than inspected as
    record attributes, because the question is what ends up in the STREAM: a
    secret sitting on `record.request` is a secret a naive formatter publishes.
    """
    with caplog.at_level(logging.INFO):
        response = failing_client.post(
            f"{BOOM}?token={TOKEN}",
            data=SECRET_PAYLOAD,
            content_type="application/json",
        )

    assert response.status_code == 500

    formatter = JSONFormatter()
    stream = "\n".join(formatter.format(record) for record in caplog.records)

    # The positive control first. Asserting only absence proves nothing if
    # nothing was logged at all.
    assert USERNAME in stream, "nothing was logged; the absences below are vacuous"
    assert "deliberate failure for the logging test" in stream

    assert PASSWORD not in stream, "the request body reached the log"
    assert TOKEN not in stream, "the query string or the body reached the log"


@override_settings(ROOT_URLCONF="tests.testapp.urls")
@pytest.mark.django_db
def test_every_logged_line_is_one_parseable_json_object(failing_client, caplog):
    with caplog.at_level(logging.INFO):
        failing_client.post(BOOM, data=SECRET_PAYLOAD, content_type="application/json")

    formatter = JSONFormatter()
    for record in caplog.records:
        line = formatter.format(record)
        assert "\n" not in line
        json.loads(line)


# ---------------------------------------------------------------------------
# Per-layer configuration
# ---------------------------------------------------------------------------

PRODUCTION_ENV = {
    "DJANGO_SECRET_KEY": "not-a-real-key-for-tests",
    "DJANGO_ALLOWED_HOSTS": "example.com",
}

FORMATTER = "settings.LOGGING['handlers']['console']['formatter']"
ROOT_LEVEL = "settings.LOGGING['root']['level']"

# Asserted against the RUNNING logging tree rather than the dictionary that
# configured it. dictConfig is applied twice — Django's DEFAULT_LOGGING first,
# this project's second — so what a logger ended up with is a different
# question from what this project asked for.
ROOT_FORMATTER = "type(__import__('logging').getLogger().handlers[0].formatter).__name__"
DJANGO_HANDLERS = "[type(h).__name__ for h in __import__('logging').getLogger('django').handlers]"


def test_production_logs_json(load_settings):
    code, out, err = load_settings("production", PRODUCTION_ENV, FORMATTER)

    assert code == 0, err
    assert json.loads(out) == "json"


def test_production_really_installs_the_json_formatter(load_settings):
    code, out, err = load_settings("production", PRODUCTION_ENV, ROOT_FORMATTER)

    assert code == 0, err
    assert json.loads(out) == "JSONFormatter"


def test_development_logs_prose(load_settings):
    code, out, err = load_settings("development", {}, FORMATTER)

    assert code == 0, err
    assert json.loads(out) == "console"


def test_development_can_preview_the_production_format(load_settings):
    """The mitigation for running two formats at all.

    Without it, the format that matters is the one nobody looks at until it is
    deployed.
    """
    code, out, err = load_settings("development", {"DJANGO_LOG_FORMAT": "json"}, ROOT_FORMATTER)

    assert code == 0, err
    assert json.loads(out) == "JSONFormatter"


def test_production_can_be_switched_to_prose_during_an_incident(load_settings):
    code, out, err = load_settings(
        "production", {**PRODUCTION_ENV, "DJANGO_LOG_FORMAT": "console"}, FORMATTER
    )

    assert code == 0, err
    assert json.loads(out) == "console"


def test_an_unknown_format_refuses_to_start(load_settings):
    code, _, err = load_settings("production", {**PRODUCTION_ENV, "DJANGO_LOG_FORMAT": "yaml"})

    assert code != 0
    assert "DJANGO_LOG_FORMAT" in err
    assert "ImproperlyConfigured" in err


def test_the_default_level_is_info(load_settings):
    code, out, err = load_settings("production", PRODUCTION_ENV, ROOT_LEVEL)

    assert code == 0, err
    assert json.loads(out) == "INFO"


def test_the_level_is_configurable_by_environment_variable(load_settings):
    code, out, err = load_settings(
        "production", {**PRODUCTION_ENV, "DJANGO_LOG_LEVEL": "debug"}, ROOT_LEVEL
    )

    assert code == 0, err
    # Lower case in, upper case out: logging's level names are upper case and
    # dictConfig raises on "debug".
    assert json.loads(out) == "DEBUG"


def test_sql_is_never_logged_even_at_debug(load_settings):
    """`django.db.backends` at DEBUG prints every statement WITH ITS BOUND
    PARAMETERS — every password and token the application has ever written.

    Pinned at INFO on purpose, so asking for more detail during an incident
    cannot arm it by accident.
    """
    code, out, err = load_settings(
        "production",
        {**PRODUCTION_ENV, "DJANGO_LOG_LEVEL": "DEBUG"},
        "settings.LOGGING['loggers']['django.db.backends']['level']",
    )

    assert code == 0, err
    assert json.loads(out) == "INFO"


def test_uvicorns_access_log_is_silenced(load_settings):
    """RequestIDMiddleware owns the request line, because it is the only layer
    that can see the correlation identifier. Two lines per request, one of them
    uncorrelated, is worse than one.
    """
    code, out, err = load_settings(
        "production", PRODUCTION_ENV, "settings.LOGGING['loggers']['uvicorn.access']"
    )

    assert code == 0, err
    config = json.loads(out)
    assert config["handlers"] == []
    assert config["propagate"] is False


def test_existing_loggers_are_not_disabled(load_settings):
    """The great silent log-loss bug: dictConfig defaults this to True, which
    switches off every logger created by an import that already ran.
    """
    code, out, err = load_settings(
        "production", PRODUCTION_ENV, "settings.LOGGING['disable_existing_loggers']"
    )

    assert code == 0, err
    assert json.loads(out) is False


def test_logs_go_to_stdout_and_never_to_a_file(load_settings):
    code, out, err = load_settings("production", PRODUCTION_ENV, "settings.LOGGING['handlers']")

    assert code == 0, err
    handlers = json.loads(out)
    # A log file written inside a container is deleted with the container, and
    # the one time you want it is after the container is gone.
    assert list(handlers) == ["console"]
    assert handlers["console"]["class"] == "logging.StreamHandler"
    assert handlers["console"]["stream"] == "ext://sys.stdout"


def test_mail_admins_is_not_inherited_from_djangos_defaults(load_settings):
    """Django applies DEFAULT_LOGGING first and this project's second, and
    dictConfig strips a named logger's existing handlers. Naming `django` here
    is what removes the AdminEmailHandler this project never configured.
    """
    code, out, err = load_settings("production", PRODUCTION_ENV, DJANGO_HANDLERS)

    assert code == 0, err
    assert json.loads(out) == []


def test_the_built_config_carries_the_health_check_filter(load_settings):
    """The regression this test exists for actually shipped.

    M6-01 put its probe suppression in a literal `LOGGING` dict in base.py.
    M6-04 then had every layer assign `LOGGING = build_logging(...)`, which
    overrides that dict — so M6-01's filter became dead code and the probes came
    back, in a merge where neither side's tests failed on its own branch.

    Asserted against the layer's FINAL settings, not against base.py, because
    that override is precisely the step that broke it.
    """
    code, out, err = load_settings(
        "production",
        PRODUCTION_ENV,
        "settings.LOGGING['loggers'].get('django.request', {}).get('filters', [])",
    )

    assert code == 0, err
    assert "suppress_health_checks" in json.loads(out), (
        "django.request logs every 4xx and 5xx, so without this filter a database "
        "outage makes every readiness probe emit an ERROR line from every replica"
    )


def test_the_correlation_middleware_runs_first(load_settings):
    """Middleware wraps in list order, so the first entry is the outermost. Any
    later position leaves the middleware above it uncorrelated.
    """
    code, out, err = load_settings("production", PRODUCTION_ENV, "settings.MIDDLEWARE[0]")

    assert code == 0, err
    assert json.loads(out) == "apps.core.middleware.RequestIDMiddleware"


def test_the_excluded_paths_list_holds_the_probe_paths(load_settings):
    """M6-01 has landed, so the list this file once required to be EMPTY is filled.

    It is the only thing keeping probe lines out of the request log:
    `uvicorn.access` is silenced outright, so RequestIDMiddleware's line is the
    sole one per request. Empty here means one line per probe, per replica,
    every few seconds, forever.

    Asserted against HEALTH_CHECK_PATHS rather than a literal, because the point
    is that the two cannot drift — a hard-coded pair here would keep passing
    after the URLconf moved.
    """
    code, out, err = load_settings(
        "production",
        PRODUCTION_ENV,
        "[settings.REQUEST_LOG_EXCLUDED_PATHS, list(settings.HEALTH_CHECK_PATHS)]",
    )

    assert code == 0, err
    excluded, health_paths = json.loads(out)
    assert excluded == health_paths, (
        "the request log must exclude exactly the probe paths, derived from "
        "HEALTH_CHECK_PATHS rather than repeated"
    )
