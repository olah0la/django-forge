"""Concrete models used only to test the abstract bases.

The bases in apps/core/models.py create no tables and cannot be queried, so
every behavioural assertion — timestamps populating, primary keys generating,
ordering — needs a real subclass to act on.

One model per base, so a failure names which base is broken rather than
implicating all three.
"""

from django.db import models

from apps.core.models import BaseModel, TimeStampedModel, UUIDModel


class UUIDThing(UUIDModel):
    """UUIDModel alone: a UUIDv7 primary key, no timestamps."""

    label = models.CharField(max_length=50, blank=True)


class StampedThing(TimeStampedModel):
    """TimeStampedModel alone: timestamps over Django's default integer key."""

    label = models.CharField(max_length=50, blank=True)


class Thing(BaseModel):
    """The combined base — what a real model in a derived project inherits."""

    label = models.CharField(max_length=50, blank=True)
