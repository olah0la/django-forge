"""The abstract base models, and the UUIDv7 generator underneath them.

Split in two: pure assertions about the generator and about model *structure*
need no database, while behaviour — timestamps populating, rows ordering —
needs one. The database-backed tests run on SQLite under `make test` and on
real PostgreSQL under `make test-db`, unchanged.
"""

import time
import uuid

import pytest
from django.db import connection

from apps.core.models import BaseModel, TimeStampedModel, UUIDModel
from apps.core.uuid7 import uuid7
from tests.testapp.models import StampedThing, Thing, UUIDThing


# ---------------------------------------------------------------------------
# The generator
# ---------------------------------------------------------------------------
def test_uuid7_reports_version_7():
    assert uuid7().version == 7


def test_uuid7_sets_the_rfc_4122_variant():
    """A wrong variant still parses and still looks like a UUID.

    Nothing raises; it is simply not a conforming UUIDv7, and the bug survives
    every casual inspection. Hence an explicit assertion.
    """
    assert uuid7().variant == uuid.RFC_4122


def test_uuid7_leads_with_the_current_millisecond():
    """The first 48 bits are a Unix-epoch millisecond timestamp."""
    before = time.time_ns() // 1_000_000
    value = uuid7().int >> 80
    after = time.time_ns() // 1_000_000
    assert before <= value <= after


def test_uuid7_does_not_collide():
    ids = [uuid7() for _ in range(50_000)]
    assert len(set(ids)) == len(ids)


def test_uuid7_sorts_by_creation_time_across_milliseconds():
    """Sortability is the whole reason for v7 over v4.

    Compared across millisecond boundaries deliberately: within a single
    millisecond the low bits are random and the order is NOT defined. Asserting
    a total ordering would be asserting something the generator does not
    promise, and would fail intermittently — measured at roughly half of
    adjacent pairs when generating in a tight loop.
    """
    samples = []
    for _ in range(5):
        samples.append(uuid7())
        time.sleep(0.002)

    assert samples == sorted(samples)


def test_uuid7_is_not_ordered_within_a_millisecond():
    """Documents the limitation as a fact, so nobody relies on the opposite.

    If this ever fails, the generator gained sub-millisecond monotonicity —
    good news, but the docstrings in apps/core/uuid7.py promise the weaker
    guarantee and would need updating alongside it.
    """
    batch = [uuid7() for _ in range(1000)]
    same_ms = [u for u in batch if (u.int >> 80) == (batch[0].int >> 80)]

    assert len(same_ms) > 10, "batch spanned too many milliseconds to test this"
    assert same_ms != sorted(same_ms)


# ---------------------------------------------------------------------------
# abstract = True, and no table for the bases
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("model", [UUIDModel, TimeStampedModel, BaseModel])
def test_bases_are_abstract(model):
    assert model._meta.abstract is True


@pytest.mark.django_db
def test_no_table_exists_for_any_abstract_base():
    """The acceptance criterion, checked against the database itself.

    `_meta.abstract` is what Django was *told*; the table list is what it
    actually did. A model that was abstract and somehow still produced a table
    would pass the first check and fail this one, which is the failure worth
    catching.
    """
    tables = set(connection.introspection.table_names())

    for base in (UUIDModel, TimeStampedModel, BaseModel):
        assert base._meta.db_table not in tables, (
            f"{base.__name__} is abstract but has a table"
        )

    # The control: a concrete subclass DOES get one, so the assertion above is
    # not passing merely because nothing was created at all.
    assert Thing._meta.db_table in tables


# ---------------------------------------------------------------------------
# Inheritance gives a concrete model what it should
# ---------------------------------------------------------------------------
@pytest.mark.django_db
def test_inheriting_model_gets_a_uuid_primary_key():
    thing = Thing.objects.create(label="first")

    assert isinstance(thing.pk, uuid.UUID)
    assert thing.pk.version == 7
    assert Thing._meta.pk.get_internal_type() == "UUIDField"


@pytest.mark.django_db
def test_primary_keys_are_unique_across_rows():
    things = [Thing.objects.create(label=str(n)) for n in range(50)]
    assert len({t.pk for t in things}) == 50


@pytest.mark.django_db
def test_taking_only_the_timestamp_base_keeps_an_integer_key():
    """TimeStampedModel must not drag the UUID strategy in with it.

    The two are separable on purpose, and this is what proves the split is
    real rather than incidental.
    """
    stamped = StampedThing.objects.create(label="x")

    assert isinstance(stamped.pk, int)
    assert StampedThing._meta.pk.get_internal_type() == "BigAutoField"


@pytest.mark.django_db
def test_taking_only_the_uuid_base_has_no_timestamps():
    field_names = {f.name for f in UUIDThing._meta.fields}
    assert "created_at" not in field_names
    assert "updated_at" not in field_names


# ---------------------------------------------------------------------------
# Timestamps
# ---------------------------------------------------------------------------
@pytest.mark.django_db
def test_timestamps_are_populated_on_creation():
    thing = Thing.objects.create(label="x")

    assert thing.created_at is not None
    assert thing.updated_at is not None


@pytest.mark.django_db
def test_updated_at_advances_on_save_and_created_at_does_not():
    thing = Thing.objects.create(label="x")
    created, first_update = thing.created_at, thing.updated_at

    time.sleep(0.01)
    thing.label = "y"
    thing.save()
    thing.refresh_from_db()

    assert thing.updated_at > first_update
    assert thing.created_at == created


@pytest.mark.django_db
def test_queryset_update_does_not_touch_updated_at():
    """The documented gotcha, pinned as behaviour.

    auto_now is applied by Model.save(), so .update() writes the row and leaves
    updated_at stale with no error. This test exists so the claim in the
    docstring and in docs/models.md cannot quietly become false — and so the
    surprise is discovered here rather than in someone's audit log.
    """
    thing = Thing.objects.create(label="x")
    before = thing.updated_at

    time.sleep(0.01)
    Thing.objects.filter(pk=thing.pk).update(label="y")
    thing.refresh_from_db()

    assert thing.label == "y"
    assert thing.updated_at == before


@pytest.mark.django_db
def test_bulk_create_does_populate_timestamps():
    """Pins the exception to the rule, because it is genuinely surprising.

    bulk_create() is routinely lumped in with update() as "bypasses auto_now",
    and it does not: Django calls each field's pre_save() while compiling the
    INSERT, so both fields are set on the instances and in the database. Only
    update() and bulk_update() skip them. Asserted so the documentation cannot
    drift back to the intuitive-but-wrong version.
    """
    things = Thing.objects.bulk_create([Thing(label=str(n)) for n in range(3)])

    assert all(t.created_at is not None for t in things)
    assert all(t.updated_at is not None for t in things)
    assert Thing.objects.get(pk=things[0].pk).updated_at == things[0].updated_at


@pytest.mark.django_db
def test_bulk_update_does_not_touch_updated_at():
    """The other half of the real gotcha, alongside .update()."""
    thing = Thing.objects.create(label="x")
    before = thing.updated_at

    time.sleep(0.01)
    thing.label = "y"
    Thing.objects.bulk_update([thing], ["label"])
    thing.refresh_from_db()

    assert thing.label == "y"
    assert thing.updated_at == before
