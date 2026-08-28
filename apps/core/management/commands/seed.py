"""Load a known development state into the database.

An empty database is a poor development environment: everyone hand-creates an
admin and a few records, differently each time, and "it works for me" gets
harder to diagnose. This command produces the same starting point for everyone,
and can be run repeatedly without accumulating duplicates.

A management command rather than JSON fixtures (tradeoff 41): fixtures break
every time a model field changes and rot silently until someone needs them,
whereas this is ordinary code that is updated alongside the models.

    make seed

It refuses to run unless settings.SEED_ENABLED is true, which only the
development and test layers set. See base.py for why that is a code-level layer
decision and not an environment variable.
"""

import os

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group, Permission
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

# Django's own names, so `createsuperuser --noinput` works from the same
# variables and nobody has to learn a second set.
#
# The defaults are working credentials. That is safe ONLY because of the
# SEED_ENABLED guard below — the two are a pair, and weakening either one
# without the other ships a default password to production.
SUPERUSER_USERNAME = os.environ.get("DJANGO_SUPERUSER_USERNAME", "admin")
SUPERUSER_EMAIL = os.environ.get("DJANGO_SUPERUSER_EMAIL", "admin@example.com")
SUPERUSER_PASSWORD = os.environ.get("DJANGO_SUPERUSER_PASSWORD", "admin")  # noqa: S105

# Groups generic enough for any Django project, rather than invented domain
# roles a derived project would have to delete. They are also two genuinely
# different permission queries — a broad predicate and a targeted per-model set
# — which is what makes them worth seeding rather than decoration.
READ_ONLY_GROUP = "read-only"
USER_ADMIN_GROUP = "user-admin"


class Command(BaseCommand):
    help = "Load development seed data (superuser and permission groups)."

    def handle(self, *args, **options):
        # Imported here rather than at module scope so @override_settings works
        # in tests: a module-level read would capture the value at import time,
        # before any override is applied, and the guard's own test would pass
        # for the wrong reason.
        from django.conf import settings

        if not getattr(settings, "SEED_ENABLED", False):
            raise CommandError(
                "Refusing to seed: SEED_ENABLED is False.\n\n"
                f"  Active settings: {os.environ.get('DJANGO_SETTINGS_MODULE', '(unset)')}\n\n"
                "  Seeding is a development convenience and creates a user with a\n"
                "  known password. Only the development and test layers enable it,\n"
                "  and deliberately not through an environment variable — a .env is\n"
                "  read by both Compose profiles, so a stray value there could arm\n"
                "  this in production.\n\n"
                "  If you meant to seed, run it against the development settings:\n"
                "      make seed"
            )

        # One transaction: a seed that half-applies leaves a state nobody
        # designed, and the next run would be reconciling against it.
        with transaction.atomic():
            self._seed_superuser()
            self._seed_groups()

            # ---------------------------------------------------------------
            # EXTENSION POINT — add your project's own seed data here.
            #
            # Keep it idempotent: get_or_create / update_or_create on a natural
            # key, and .set() rather than .add() for many-to-many fields, so a
            # second run converges instead of accumulating.
            # ---------------------------------------------------------------

        self.stdout.write("")
        self.stdout.write(
            self.style.SUCCESS(
                f"  Seed complete. Log in as '{SUPERUSER_USERNAME}' "
                f"with password '{SUPERUSER_PASSWORD}'."
            )
        )
        self.stdout.write("")

    # -----------------------------------------------------------------------
    def _seed_superuser(self):
        """Ensure the development superuser exists, is privileged, and works.

        get_user_model() rather than auth.User: the roadmap defers a custom user
        model to a later phase, and this command should survive that landing
        without edits.
        """
        user_model = get_user_model()
        username_field = user_model.USERNAME_FIELD

        user, created = user_model.objects.get_or_create(
            **{username_field: SUPERUSER_USERNAME},
            defaults={"email": SUPERUSER_EMAIL},
        )

        # Re-asserted on every run, not only at creation. The point of this
        # command is a KNOWN state, and a user whose flags were changed by
        # hand — or whose password was — is not the state that is documented.
        # It is reported rather than done silently, because resetting a
        # password someone deliberately changed is a surprise worth naming.
        user.is_staff = True
        user.is_superuser = True
        user.set_password(SUPERUSER_PASSWORD)
        user.save()

        if created:
            self.stdout.write(f"  created superuser  {SUPERUSER_USERNAME}")
        else:
            self.stdout.write(f"  superuser exists   {SUPERUSER_USERNAME} (password reset)")

    # -----------------------------------------------------------------------
    def _seed_groups(self):
        """Create the permission groups and set their permissions.

        `.set()` rather than `.add()` throughout: set() makes the group's
        permissions match exactly what is listed here, so removing one from the
        list actually removes it on the next run. add() would only ever grow the
        set, which is how a "read-only" group quietly acquires write access.
        """
        # Everything viewable, nothing else. The natural shape of support or
        # analyst access, and it stays correct as models are added.
        read_only_permissions = Permission.objects.filter(codename__startswith="view_")
        self._ensure_group(READ_ONLY_GROUP, read_only_permissions)

        # Managing accounts without granting anything else. Deliberately no
        # delete_*: removing a user is rarely what is wanted (it cascades), and
        # a group handed out freely should not be able to do it.
        user_admin_permissions = Permission.objects.filter(
            content_type__app_label="auth",
            content_type__model__in=["user", "group"],
            codename__regex=r"^(add|change|view)_",
        )
        self._ensure_group(USER_ADMIN_GROUP, user_admin_permissions)

    # -----------------------------------------------------------------------
    def _ensure_group(self, name, permissions):
        group, created = Group.objects.get_or_create(name=name)
        group.permissions.set(permissions)

        verb = "created group    " if created else "group exists     "
        self.stdout.write(f"  {verb} {name} ({permissions.count()} permissions)")
