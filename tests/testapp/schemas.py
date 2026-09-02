"""Schemas for the test-only app. Test layer only.

`apps/core/schemas.py` is the shipped worked example for M5-03. This is the
minimum needed to give the pagination fixture (M5-04) a typed response, and it
follows the same conventions: an allow-list, and a name that says which
direction it travels.
"""

from ninja import ModelSchema

from tests.testapp.models import Thing


class ThingOut(ModelSchema):
    class Meta:
        model = Thing
        fields = ["id", "label", "created_at"]
