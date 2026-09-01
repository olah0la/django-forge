"""The schema conventions — the separation, and the allow-list.

Two kinds of test here, and the second kind matters more.

The round trip proves the worked example in `apps/core/schemas.py` works: a user
is created, read and patched through real HTTP, and no response carries a
password. That is the demonstration.

The structural tests are the *guard*, and they are written to outlive the
example. They fail on a schema written years from now that reaches for
`fields = "__all__"` or `exclude`, because the danger those shortcuts create is
not in the code that adds them — it is in the model field somebody adds
afterwards, which appears in the API with nothing to review.
"""

import importlib
from pathlib import Path

import pytest
from django.contrib.auth.models import User
from django.test import Client
from ninja import ModelSchema

from apps.core.schemas import UserCreateIn, UserOut, UserUpdateIn

REPO_ROOT = Path(__file__).resolve().parent.parent
BASE = "/api/v1"

# Every test that speaks HTTP needs the throwaway URLconf: the shipped API
# mounts no endpoint for these schemas, deliberately. pytest-django's `urls`
# marker swaps ROOT_URLCONF for the duration. See tests/testapp/urls.py.
pytestmark = pytest.mark.urls("tests.testapp.urls")


@pytest.fixture
def client():
    return Client()


# ---------------------------------------------------------------------------
# The shipped schemas, walked generically
# ---------------------------------------------------------------------------
def _shipped_schema_modules():
    """Every `schemas` module under apps/, imported.

    Both shapes the conventions allow: `apps/<name>/schemas.py`, and the
    package form an app grows into, `apps/<name>/schemas/__init__.py`.
    """
    paths = [
        *(REPO_ROOT / "apps").rglob("schemas.py"),
        *(REPO_ROOT / "apps").rglob("schemas/__init__.py"),
    ]
    for path in paths:
        parts = path.relative_to(REPO_ROOT).with_suffix("").parts
        if parts[-1] == "__init__":
            parts = parts[:-1]
        yield importlib.import_module(".".join(parts))


def _shipped_model_schemas():
    """(name, class) for every ModelSchema defined under apps/."""
    for module in _shipped_schema_modules():
        for name, obj in vars(module).items():
            if isinstance(obj, type) and issubclass(obj, ModelSchema) and obj is not ModelSchema:
                yield f"{module.__name__}.{name}", obj


def test_there_are_shipped_schemas_to_check():
    """Guards the guards.

    Every structural test below iterates over what this finds. If the discovery
    ever returns nothing — a renamed module, a moved directory — those tests
    would pass vacuously, which is worse than failing.
    """
    assert dict(_shipped_model_schemas())


def test_no_schema_uses_the_include_everything_shortcut():
    """M5-03 criterion 2, enforced rather than documented.

    `fields = "__all__"` is not wrong the day it is written — it is wrong the
    day a field is added to the model. There is no diff for a reviewer to catch
    at that point, because the exposure happens in a file nobody touched.
    """
    for name, schema in _shipped_model_schemas():
        assert getattr(schema.Meta, "fields", None) != "__all__", name


def test_no_schema_uses_exclude():
    """The same defect wearing a responsible-looking hat.

    `exclude = ["password"]` reads as a security measure and is the opposite of
    one: it enumerates today's known-dangerous fields and admits everything
    else, forever. An allow-list fails closed; a deny-list fails open.
    """
    for name, schema in _shipped_model_schemas():
        assert not getattr(schema.Meta, "exclude", None), name


def test_schema_names_carry_their_direction():
    """M5-03 criterion 1.

    Direction in the name is what makes a schema used the wrong way visible at
    the call site — `response=UserCreateIn` reads as obviously wrong, where
    `response=User` would not.
    """
    for name, _schema in _shipped_model_schemas():
        assert name.endswith(("In", "Out")), f"{name} says nothing about its direction"


# ---------------------------------------------------------------------------
# The allow-list, and the leak it prevents
# ---------------------------------------------------------------------------
def test_user_out_field_set_is_pinned():
    """Pinned exactly, so a widened response has to be a deliberate act.

    Asserting only that `password` is absent would keep passing while the
    response quietly grew `is_staff`, `is_superuser` or `last_login`. Equality
    means adding a field to the model — or to this schema — fails here first,
    and somebody decides.
    """
    assert set(UserOut.model_fields) == {"id", "username", "email", "date_joined"}


def test_the_leak_the_allow_list_prevents_is_real():
    """Measured, not remembered.

    The counter-example is built here on purpose: the argument for allow-lists
    rests on what `"__all__"` actually produces, and that is a claim about the
    library, which can change. If Ninja ever stops including these, this test
    fails and the documentation gets revisited rather than repeated.
    """

    class UserEverything(ModelSchema):
        class Meta:
            model = User
            fields = "__all__"

    leaked = set(UserEverything.model_fields)

    assert {"password", "is_superuser", "is_staff", "last_login"} <= leaked
    assert leaked & set(UserOut.model_fields) == set(UserOut.model_fields)
    assert "password" not in UserOut.model_fields


def test_create_and_update_are_different_types():
    """M5-03 criterion 1, and the reason the separation is not duplication.

    A client may set a username when creating and may not change it afterwards;
    a client sends a password on create and changes it through its own
    operation. One shared schema cannot say either of those things.
    """
    assert "username" in UserCreateIn.model_fields
    assert "username" not in UserUpdateIn.model_fields
    assert "password" in UserCreateIn.model_fields
    assert "password" not in UserUpdateIn.model_fields


# ---------------------------------------------------------------------------
# The round trip, over real HTTP
# ---------------------------------------------------------------------------
@pytest.mark.django_db
def test_create_returns_the_output_schema_without_the_password(client):
    """M5-03 criterion 4 — create.

    The response is built from a type that never listed `password`, so nothing
    in the view has to remember to strip it. That is the entire argument for
    separate types, and it is checked on the response body rather than on the
    schema, because the body is what a client receives.
    """
    response = client.post(
        f"{BASE}/users/",
        data={"username": "ada", "email": "ada@example.com", "password": "correct-horse"},
        content_type="application/json",
    )

    assert response.status_code == 200, response.content
    body = response.json()

    assert set(body) == {"id", "username", "email", "date_joined"}
    assert "password" not in body
    assert body["username"] == "ada"


@pytest.mark.django_db
def test_the_password_is_hashed_rather_than_stored(client):
    """Not a schema concern, and exactly why the endpoint is not a passthrough.

    The schema carries the plaintext a client sent; the column holds a hash.
    They share a name and are different things, which is why `password` is
    declared explicitly on the input schema rather than pulled from the model.
    """
    client.post(
        f"{BASE}/users/",
        data={"username": "ada", "email": "ada@example.com", "password": "correct-horse"},
        content_type="application/json",
    )

    user = User.objects.get(username="ada")
    assert user.password != "correct-horse"
    assert user.check_password("correct-horse")


@pytest.mark.django_db
def test_read_returns_exactly_the_allow_listed_fields(client):
    """M5-03 criterion 4 — read."""
    user = User.objects.create_user("ada", "ada@example.com", "correct-horse")

    body = client.get(f"{BASE}/users/{user.id}").json()

    assert set(body) == {"id", "username", "email", "date_joined"}


@pytest.mark.django_db
def test_patch_changes_only_what_the_client_sent(client):
    """M5-03 criterion 4 — update, and the PatchDict claim.

    `first_name` is in UserUpdateIn and is NOT in this request. With a plain
    all-optional schema it would arrive as None, indistinguishable from an
    explicit null, and the view's loop would blank it out. This is the test
    that would fail if someone simplified PatchDict away.
    """
    user = User.objects.create_user("ada", "ada@example.com", "correct-horse")
    user.first_name = "Ada"
    user.save()

    response = client.patch(
        f"{BASE}/users/{user.id}",
        data={"email": "ada@lovelace.example"},
        content_type="application/json",
    )

    assert response.status_code == 200, response.content
    user.refresh_from_db()
    assert user.email == "ada@lovelace.example"
    assert user.first_name == "Ada", "an unsent field was overwritten"


@pytest.mark.django_db
def test_an_empty_patch_changes_nothing(client):
    """The degenerate case, which is where the ambiguity is easiest to see.

    An empty body means "change nothing". Under an all-optional schema it means
    "set every field to null", and the difference is invisible until a support
    ticket says a user lost their name.
    """
    user = User.objects.create_user("ada", "ada@example.com", "correct-horse")
    user.first_name = "Ada"
    user.save()

    client.patch(f"{BASE}/users/{user.id}", data={}, content_type="application/json")

    user.refresh_from_db()
    assert user.first_name == "Ada"
    assert user.email == "ada@example.com"


@pytest.mark.django_db
def test_input_validation_rejects_a_bad_password_before_the_view_runs(client):
    """The schema is the first gate, and it answers 422 without touching the DB.

    Not a password policy — Django's validators are that. This is the schema
    doing what schemas do: refusing input that does not match the declared
    contract, generated straight from the type hint.
    """
    response = client.post(
        f"{BASE}/users/",
        data={"username": "ada", "email": "ada@example.com", "password": "short"},
        content_type="application/json",
    )

    assert response.status_code == 422
    assert not User.objects.filter(username="ada").exists()


# ---------------------------------------------------------------------------
# What the schemas do to the OpenAPI document
# ---------------------------------------------------------------------------
@pytest.mark.django_db
def test_both_directions_appear_separately_in_the_document(client):
    """Separate types produce separate components, which is what clients see.

    A shared schema generates one component used for both request and response,
    so a generated client offers `password` on the way out and accepts `id` on
    the way in. The separation is visible here, in the artefact consumers
    actually read.
    """
    components = client.get(f"{BASE}/openapi.json").json()["components"]["schemas"]

    assert {"UserCreateIn", "UserOut"} <= set(components)
    assert "password" in components["UserCreateIn"]["properties"]
    assert "password" not in components["UserOut"]["properties"]


@pytest.mark.django_db
def test_patch_dict_publishes_its_own_component(client):
    """`PatchDict[UserUpdateIn]` appears as `UserUpdateInPatch`, all optional.

    Worth pinning because it is client-visible: the name in the document is not
    the name in the code, and a generated client will use the published one.
    """
    components = client.get(f"{BASE}/openapi.json").json()["components"]["schemas"]

    assert "UserUpdateInPatch" in components
    assert not components["UserUpdateInPatch"].get("required")


@pytest.mark.django_db
def test_model_help_text_becomes_public_api_documentation(client):
    """A cost of deriving schemas from models, measured rather than asserted.

    Django's `help_text` is written for the admin and for form users. Through
    ModelSchema it lands in the public OpenAPI document verbatim — here,
    User.username's admin help text describes the API's contract. Usually
    harmless, occasionally not: help text mentioning internal systems, or
    contradicting what the endpoint actually accepts, ships to consumers.
    """
    components = client.get(f"{BASE}/openapi.json").json()["components"]["schemas"]
    described = components["UserOut"]["properties"]["username"]["description"]

    assert described == str(User._meta.get_field("username").help_text)
    assert "150 characters or fewer" in described
