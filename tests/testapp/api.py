"""A router used only to exercise the composition pattern. Test layer only.

The shipped demonstration of the pattern is `apps/core/api.py`, and it is the
documented exception: it mounts at the API root with an empty prefix. So the
*prefix* half of the convention has nothing in the shipped code to assert
against.

This is that missing half — a router shaped exactly like a feature app's, which
tests mount on a throwaway instance. It stays here rather than becoming a real
app under `apps/` because an app shipped purely as a demonstration is one every
derived project has to delete; M7-04 owns the removable worked example.

**Never mounted on the real API instance.** It is imported by tests only.
"""

from django.http import HttpRequest
from ninja import Router

# The convention a feature app follows: one tag, named for the app, set on the
# router so every operation inherits it.
router = Router(tags=["things"])


@router.get("/", summary="List things")
def list_things(request: HttpRequest) -> list[dict]:
    return [{"label": "a thing"}]
