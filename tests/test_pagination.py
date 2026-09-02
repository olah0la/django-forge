"""Pagination — the envelope, the ceiling, and the rule that every list obeys it.

The endpoint under test lives in `tests/testapp/api.py`: the shipped API serves
meta endpoints only, so there is nothing there to paginate yet. What is shipped
and being tested is the *configuration* — `NINJA_PAGINATION_*` in
`config/settings/base.py` — and the `RouterPaginated` convention that applies it.

`override_settings` is deliberately absent. Ninja reads its pagination settings
once, when `ninja.conf` is imported, and bakes the maximum into the query
parameter's validation at class-definition time. Overriding them in a test
changes nothing and would produce assertions that pass against a fiction. These
tests run against the real, configured values.
"""

import pytest
from django.test import Client

from tests.testapp.models import Thing

BASE = "/api/v1"
THINGS = f"{BASE}/things/"

# The configured values, restated so a failure names what changed. Kept in step
# with config/settings/base.py by test_the_configured_values_are_the_documented_ones.
DEFAULT_PAGE_SIZE = 25
MAX_PAGE_SIZE = 100

TOTAL = 250

pytestmark = pytest.mark.urls("tests.testapp.urls")


@pytest.fixture
def client():
    return Client()


@pytest.fixture
def things(db):
    """A result set larger than one page, in one INSERT.

    `bulk_create` still applies the model's field defaults, so every row gets a
    UUIDv7 primary key — see apps/core/models.py, where that is measured.
    """
    Thing.objects.bulk_create(Thing(label=f"thing-{n:04d}") for n in range(TOTAL))
    return Thing.objects.order_by("id")


# ---------------------------------------------------------------------------
# The envelope and the default
# ---------------------------------------------------------------------------
def test_the_envelope_is_items_and_count(client, things):
    """M5-04 criterion 2 — identical across every endpoint, because it is Ninja's.

    Pinned by equality rather than by `in`, so an extra key cannot appear
    unnoticed: the envelope is a contract, and clients destructure it.
    """
    body = client.get(THINGS).json()

    assert set(body) == {"items", "count"}
    assert body["count"] == TOTAL, "count is the size of the whole set, not of the page"


def test_a_default_page_size_applies_when_the_client_asks_for_none(client, things):
    """M5-04 criterion 4.

    The failure this prevents is not an error — it is a 200 response carrying
    every row, which looks like success until the table grows.
    """
    body = client.get(THINGS).json()

    assert len(body["items"]) == DEFAULT_PAGE_SIZE


def test_offset_moves_the_window(client, things):
    """The window follows the ORDERING, which is not the same as insertion order.

    Asserting `items[0]["label"] == "thing-0000"` here fails, and correctly: a
    UUIDv7 is time-sortable to the millisecond, and within one millisecond its
    low bits are random — 250 rows created by one `bulk_create` share a
    millisecond or two, so they do not come back in the order they were written.

    Pagination needs a *deterministic total* order, which `id` is. It does not
    need insertion order, which `id` is not. See docs/models.md.
    """
    expected = [str(thing.id) for thing in things[:20]]

    first = client.get(THINGS, {"limit": 10}).json()["items"]
    second = client.get(THINGS, {"limit": 10, "offset": 10}).json()["items"]

    assert [item["id"] for item in first] == expected[:10]
    assert [item["id"] for item in second] == expected[10:]


def test_walking_every_page_yields_each_row_exactly_once(client, things):
    """The property that makes pagination *correct*, not merely bounded.

    This is what an unordered queryset breaks: without a deterministic ORDER BY,
    PostgreSQL may return rows in a different order between two requests, so
    pages overlap and rows vanish — with nothing raising, and no error to see.
    The endpoint orders by its UUIDv7 primary key, which is a deterministic
    total order. That is the property pagination needs.
    """
    seen = []
    for offset in range(0, TOTAL, DEFAULT_PAGE_SIZE):
        seen += [item["id"] for item in client.get(THINGS, {"offset": offset}).json()["items"]]

    assert len(seen) == TOTAL
    assert len(set(seen)) == TOTAL, "a row was returned by two different pages"


def test_an_offset_past_the_end_is_an_empty_page_not_an_error(client, things):
    body = client.get(THINGS, {"offset": TOTAL * 2}).json()

    assert body["items"] == []
    assert body["count"] == TOTAL


# ---------------------------------------------------------------------------
# The ceiling (criterion 3)
# ---------------------------------------------------------------------------
def test_the_maximum_page_size_can_be_requested(client, things):
    body = client.get(THINGS, {"limit": MAX_PAGE_SIZE}).json()

    assert len(body["items"]) == MAX_PAGE_SIZE


@pytest.mark.parametrize("limit", [MAX_PAGE_SIZE + 1, 500, 1_000_000])
def test_a_client_cannot_exceed_the_maximum_page_size(client, things, limit):
    """M5-04 criterion 3, and the whole point of the issue.

    Ninja's own default for this ceiling is `inf`: with nothing set in
    config/settings/base.py, every one of these would be a 200 response
    carrying that many rows.

    REFUSED, not clamped. A client that asked for 1,000 rows, believes it got
    1,000, and actually got 100 will page through the data wrongly and never
    see an error.
    """
    response = client.get(THINGS, {"limit": limit})

    assert response.status_code == 422
    assert "less_than_equal" in response.content.decode()


@pytest.mark.parametrize("limit", [0, -1])
def test_a_nonsensical_page_size_is_refused(client, things, limit):
    assert client.get(THINGS, {"limit": limit}).status_code == 422


def test_a_negative_offset_is_refused(client, things):
    assert client.get(THINGS, {"offset": -1}).status_code == 422


def test_the_ceiling_is_published_rather_than_discovered_through_a_422(client, db):
    """A client should be able to read the limit, not find it by being rejected.

    The maximum reaches the OpenAPI document because it is a validation rule on
    the query parameter, not a check inside the view — which is the argument for
    configuring it rather than writing `if limit > 100` anywhere.
    """
    document = client.get(f"{BASE}/openapi.json").json()
    parameters = document["paths"][f"{BASE}/things/"]["get"]["parameters"]
    limit = next(p for p in parameters if p["name"] == "limit")

    assert limit["schema"]["maximum"] == MAX_PAGE_SIZE
    assert limit["schema"]["minimum"] == 1
    assert limit["schema"]["default"] == DEFAULT_PAGE_SIZE
    assert limit["required"] is False


def test_the_configured_values_are_the_documented_ones():
    """The settings this suite asserts against are the ones the project ships.

    Everything above tests behaviour through HTTP, which would keep passing if
    someone changed the numbers in base.py and the constants here together. This
    is the line that makes such a change deliberate: the documented numbers are
    25 and 100.
    """
    from django.conf import settings

    assert settings.NINJA_PAGINATION_PER_PAGE == DEFAULT_PAGE_SIZE
    assert settings.NINJA_PAGINATION_MAX_LIMIT == MAX_PAGE_SIZE
    # Ninja's alias for this one genuinely has no PAGINATION_ prefix. Spelled
    # "correctly" it is never read, and the ceiling for PageNumber/Cursor
    # pagination silently reverts to the library default.
    assert settings.NINJA_MAX_PER_PAGE_SIZE == MAX_PAGE_SIZE


# ---------------------------------------------------------------------------
# The rule, not the endpoint (criterion 1)
# ---------------------------------------------------------------------------
def _array_responses(document):
    """Every operation whose 200 response is a bare JSON array."""
    for path, methods in document["paths"].items():
        for method, operation in methods.items():
            schema = (
                operation.get("responses", {})
                .get("200", {})
                .get("content", {})
                .get("application/json", {})
                .get("schema", {})
            )
            if schema.get("type") == "array":
                yield f"{method.upper()} {path}"


@pytest.mark.parametrize("urlconf", ["config.urls", "tests.testapp.urls"])
def test_no_endpoint_returns_an_unpaginated_array(client, db, urlconf, settings):
    """The guard that outlives this endpoint and its fixtures.

    Deliberately blind to *how* an endpoint was declared: it reads the published
    contract and fails on any operation that hands a client a bare array, which
    is the shape that cannot be bounded. An endpoint that opts out of
    `RouterPaginated`, or predates it, is caught the same way.

    Both URLconfs, because the shipped one has no list endpoints yet — checking
    only the fixture would leave the real API unguarded the day it grows one.
    """
    settings.ROOT_URLCONF = urlconf

    unpaginated = list(_array_responses(client.get(f"{BASE}/openapi.json").json()))

    assert not unpaginated, f"unpaginated list endpoints: {unpaginated}"


def test_a_non_collection_endpoint_is_left_alone(client, db):
    """RouterPaginated touches only operations whose response is a collection.

    `/ping` returns an object and gains no limit/offset parameters — worth
    pinning, because "every router is paginated" reads as though it would.
    """
    document = client.get(f"{BASE}/openapi.json").json()

    assert document["paths"][f"{BASE}/ping"]["get"].get("parameters", []) == []
