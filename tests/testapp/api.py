"""Routers used only by tests. Test layer only, never mounted on the real API.

The shipped demonstration of the pattern is `apps/core/api.py`, and it is the
documented exception: it mounts at the API root with an empty prefix. So the
*prefix* half of the convention has nothing in the shipped code to assert
against.

`router` below is that missing half — shaped exactly like a feature app's, and
mounted on a throwaway instance by the tests. `users_router` is the second half
of the same argument for M5-03: it exercises the shipped schemas through real
HTTP without the project shipping a user API. `logging_router` is the third:
M6-04 has to prove a secret never reaches a log, which needs an endpoint that
receives one and then fails — something no shipped endpoint may be.

Both stay here rather than becoming real apps under `apps/`, because an app
shipped purely as a demonstration is one every derived project has to delete;
M7-04 owns the removable worked example.

**Never mounted on the real API instance.** They are imported by tests only —
tests/testapp/urls.py mounts them on an instance of their own.
"""

import logging

from django.contrib.auth.models import User
from django.db.models import QuerySet
from django.http import HttpRequest
from django.shortcuts import get_object_or_404
from ninja import PatchDict, Router, Schema
from ninja.pagination import RouterPaginated

from apps.core.schemas import UserCreateIn, UserOut, UserUpdateIn
from tests.testapp.models import Thing
from tests.testapp.schemas import ThingOut

# The convention a feature app follows: `RouterPaginated`, and one tag named for
# the app, set on the router so every operation inherits it.
router = RouterPaginated(tags=["things"])


@router.get("/", response=list[ThingOut], summary="List things")
def list_things(request: HttpRequest) -> QuerySet[Thing]:
    """A list endpoint with no pagination code in it (M5-04).

    `RouterPaginated` sees a collection `response=` and injects the `limit` and
    `offset` parameters, the ceiling, and the `{items, count}` envelope. There
    is no decorator to forget.

    ORDER BY IS LOAD-BEARING. Offset pagination over an unordered queryset is
    undefined — PostgreSQL may return rows in any order it likes between two
    requests, so page 2 can repeat rows from page 1 and omit others entirely,
    with nothing raising.

    A UUIDv7 primary key gives a deterministic TOTAL order, which is what
    pagination requires. It is chronological only to the millisecond — rows
    written in the same millisecond come back in an arbitrary (but stable)
    order, so this is not insertion order. See docs/models.md.
    """
    return Thing.objects.order_by("id")


# ---------------------------------------------------------------------------
# The schema round trip (M5-03)
# ---------------------------------------------------------------------------
# `apps/core/schemas.py` ships the worked example with no endpoints attached,
# deliberately: authentication is still a stub (M5-07), so a real unauthenticated
# user API must not exist. These endpoints are how those schemas are exercised
# anyway — the SHIPPED types, driven through real HTTP, mounted only by
# tests/testapp/urls.py.
users_router = Router(tags=["users"])


@users_router.post("/", response=UserOut, summary="Create a user")
def create_user(request: HttpRequest, payload: UserCreateIn) -> User:
    """Input and output are different types, and this is where it shows.

    The payload carries a password; the response cannot contain it, because
    UserOut does not list it. No filtering happens here — the separation does
    the work.

    `.dict()` rather than attribute access, because mypy cannot see fields that
    ModelSchema derives from the model at runtime: `payload.username` is an
    attr-defined error even though it resolves correctly. See docs/api.md.
    """
    data = payload.dict()
    password = data.pop("password")  # the plaintext never reaches the column

    user = User(**data)
    user.set_password(password)
    user.save()
    return user


@users_router.get("/{user_id}", response=UserOut, summary="Read a user")
def read_user(request: HttpRequest, user_id: int) -> User:
    return get_object_or_404(User, id=user_id)


@users_router.patch("/{user_id}", response=UserOut, summary="Update a user")
def update_user(request: HttpRequest, user_id: int, payload: PatchDict[UserUpdateIn]) -> User:
    """`PatchDict` hands over only the keys the client actually sent.

    With a plain all-optional schema, a field the client never mentioned and a
    field explicitly set to null arrive identically, and the loop below would
    blank out every column the client did not name.
    """
    user = get_object_or_404(User, id=user_id)
    for attribute, value in payload.items():
        setattr(user, attribute, value)
    user.save()
    return user


# ---------------------------------------------------------------------------
# The redaction fixture (M6-04)
# ---------------------------------------------------------------------------
# M6-04's last acceptance criterion is that passwords and tokens are never
# logged, "verified by triggering an error on an endpoint receiving sensitive
# input". That needs an endpoint that both TAKES a secret and FAILS — a
# combination no shipped endpoint should ever have, which is exactly why it
# lives here rather than in apps/.
#
# What the failing request exercises, in one round trip:
#   * Django's `django.request` logger, which attaches the live HttpRequest to
#     the ERROR record it emits for a 500 — the leak JSONFormatter's allow-list
#     exists to close.
#   * The request log line from RequestIDMiddleware, over a URL with a token in
#     its query string.
#   * A view logging a message of its own while handling the request, so the
#     correlation identifier can be shown to reach it.
logging_router = Router(tags=["logging"])

logger = logging.getLogger("apps.testapp")


class SecretIn(Schema):
    """Input shaped like a real login: a name, and two things that must not leak."""

    username: str
    password: str
    token: str


@logging_router.post("/boom", summary="Fail while holding a secret")
def boom(request: HttpRequest, payload: SecretIn) -> None:
    """Raise, with the caller's secrets in scope at the moment it happens.

    The message deliberately mentions the username and NOT the password. A test
    that only asserts the absence of a string proves nothing if nothing was
    logged at all, so the username is the positive control: it must be present,
    and its neighbours must not.
    """
    logger.info("about to fail for %s", payload.username)
    raise RuntimeError("deliberate failure for the logging test")
