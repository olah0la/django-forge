"""The `seed` management command.

Two things are worth testing here and one of them is easy to get wrong. The
easy half is that seeding produces the documented state. The half that matters
is that it REFUSES to produce it anywhere it should not, and that it refuses
before writing anything.
"""

import json

import pytest
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group, Permission
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import override_settings

from apps.core.management.commands.seed import (
    READ_ONLY_GROUP,
    SUPERUSER_PASSWORD,
    SUPERUSER_USERNAME,
    USER_ADMIN_GROUP,
)


def _seed():
    """Run the command, discarding its output."""
    import io

    call_command("seed", stdout=io.StringIO())


# ---------------------------------------------------------------------------
# What it creates
# ---------------------------------------------------------------------------
@pytest.mark.django_db
def test_seed_creates_a_working_superuser():
    """The password is checked, not just the row.

    A user that exists but cannot log in satisfies "a superuser was created"
    and fails the thing the issue actually asks for — a usable starting point.
    """
    _seed()

    user = get_user_model().objects.get(**{get_user_model().USERNAME_FIELD: SUPERUSER_USERNAME})

    assert user.is_superuser
    assert user.is_staff
    assert user.check_password(SUPERUSER_PASSWORD)


@pytest.mark.django_db
def test_seed_creates_both_permission_groups():
    _seed()

    assert Group.objects.filter(name=READ_ONLY_GROUP).exists()
    assert Group.objects.filter(name=USER_ADMIN_GROUP).exists()


@pytest.mark.django_db
def test_read_only_group_has_every_view_permission_and_nothing_else():
    _seed()

    granted = Group.objects.get(name=READ_ONLY_GROUP).permissions.all()

    assert granted.count() == Permission.objects.filter(codename__startswith="view_").count()
    assert granted.count() > 0, "no view permissions exist at all — the query is wrong"
    assert all(p.codename.startswith("view_") for p in granted), (
        "a group named read-only granted something that is not a view permission"
    )


@pytest.mark.django_db
def test_user_admin_group_cannot_delete():
    """Deliberate: delete_user cascades, and this group is meant to be handed out."""
    granted = None
    _seed()
    granted = Group.objects.get(name=USER_ADMIN_GROUP).permissions.all()

    assert granted.count() > 0
    assert not any(p.codename.startswith("delete_") for p in granted)
    assert {p.content_type.model for p in granted} == {"user", "group"}


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------
@pytest.mark.django_db
def test_running_twice_changes_nothing_and_does_not_raise():
    """Counts the many-to-many join rows, which is where this actually breaks.

    Groups and users are created with get_or_create and are hard to duplicate.
    Permissions are the trap: `.add()` only ever grows the set, so a second run
    would leave the counts here unchanged while a *third* would too — the bug
    hides unless you count the join rows and unless the permission list is
    later shortened. Counting them is the cheap half of catching it.
    """
    _seed()
    first = (
        get_user_model().objects.count(),
        Group.objects.count(),
        sum(g.permissions.count() for g in Group.objects.all()),
    )

    _seed()
    second = (
        get_user_model().objects.count(),
        Group.objects.count(),
        sum(g.permissions.count() for g in Group.objects.all()),
    )

    assert first == second


@pytest.mark.django_db
def test_seeding_converges_when_a_group_was_modified_by_hand():
    """`.set()` semantics, stated as behaviour.

    Someone grants the read-only group a delete permission by hand. The next
    seed must take it away again — that is the difference between .set() and
    .add(), and the reason a "read-only" group can otherwise quietly acquire
    write access and keep it.
    """
    _seed()
    group = Group.objects.get(name=READ_ONLY_GROUP)
    stray = Permission.objects.filter(codename__startswith="delete_").first()
    group.permissions.add(stray)
    assert group.permissions.filter(pk=stray.pk).exists()

    _seed()

    assert not group.permissions.filter(pk=stray.pk).exists()


# ---------------------------------------------------------------------------
# The guard
# ---------------------------------------------------------------------------
@pytest.mark.django_db
@override_settings(SEED_ENABLED=False)
def test_seed_refuses_when_not_enabled():
    with pytest.raises(CommandError) as excinfo:
        _seed()

    assert "SEED_ENABLED" in str(excinfo.value)


@pytest.mark.django_db
@override_settings(SEED_ENABLED=False)
def test_refusing_writes_nothing():
    """A guard that refuses *after* writing is not a guard.

    Checked separately from the message: the command could plausibly create the
    superuser and then fail, and the test above would still pass.
    """
    with pytest.raises(CommandError):
        _seed()

    assert get_user_model().objects.count() == 0
    assert Group.objects.count() == 0


# ---------------------------------------------------------------------------
# The flag is off wherever it must be off
# ---------------------------------------------------------------------------
def test_production_layer_does_not_enable_seeding(load_settings):
    """The property the whole guard rests on, asserted against a real import."""
    rc, out, err = load_settings(
        "production",
        {
            "DJANGO_SECRET_KEY": "x" * 50,
            "DJANGO_ALLOWED_HOSTS": "example.com",
        },
        "{'enabled': settings.SEED_ENABLED}",
    )
    assert rc == 0, err
    assert json.loads(out)["enabled"] is False


def test_development_layer_enables_seeding(load_settings):
    rc, out, err = load_settings("development", {}, "{'enabled': settings.SEED_ENABLED}")
    assert rc == 0, err
    assert json.loads(out)["enabled"] is True
