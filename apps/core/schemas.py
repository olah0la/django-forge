"""Schema conventions, demonstrated on the one model every project already has.

**Read this before writing a schema.** The conventions are the point of the
file; `User` is only the vehicle. The full reasoning is in docs/api.md.

    apps/<name>/schemas.py     where schemas live, beside api.py
    XCreateIn, XUpdateIn       request bodies — every input type ends `In`
    XOut                       responses — every output type ends `Out`

**Input and output are always separate types.** One shared schema is genuinely
less code, and it does two things silently: fields meant to be read-only become
writable, and fields meant to be write-only appear in responses. `password`
below is the whole argument in one field — it exists on the way in and *cannot*
appear on the way out, because `UserOut` is a different type that does not list
it. Nothing enforces that except the separation.

**Response schemas are allow-lists.** `Meta.fields` names every field, always.
Ninja offers two shortcuts and both are deny-lists in disguise:

    fields = "__all__"        every field, including the ones added tomorrow
    exclude = ["password"]    every field EXCEPT today's known-dangerous ones

Measured against this model: `fields = "__all__"` over `User` produces
`password`, `is_superuser`, `is_staff`, `last_login` and `user_permissions`. The
failure mode is not today, though — it is the field somebody adds next year,
which appears in the API with no diff for a reviewer to catch. That mechanism is
behind a large share of real-world data exposure incidents. A test rejects both
shortcuts anywhere under `apps/`.

**Nothing here is wired to an endpoint, deliberately.** These schemas are inert:
`config/api.py` mounts no route that uses them. Authentication is still a
documented stub (M5-07), and a template that shipped a working unauthenticated
user API — create, list, update — would be shipping a vulnerability to every
project forged from it. Copy the shape; add the endpoints once you have auth.
"""

from django.contrib.auth.models import User
from ninja import Field, ModelSchema


class UserOut(ModelSchema):
    """What the API is willing to say about a user.

    An allow-list, and the shortest of the three on purpose. Everything absent
    is absent by decision: `password` (a hash is still a credential),
    `is_superuser` and `is_staff` (privilege topology is not public), and
    `last_login` (activity metadata).

    A test pins this field set exactly, so adding a field to the model cannot
    widen a response by accident — the test fails, and somebody decides.
    """

    class Meta:
        model = User
        fields = ["id", "username", "email", "date_joined"]


class UserCreateIn(ModelSchema):
    """What a client may set when creating a user.

    `password` is declared here rather than in `Meta.fields` for two reasons.
    It is the plaintext a client sends, not the hash the column stores — a
    different thing that happens to share a name — and declaring it explicitly
    is what lets it carry a validation rule. It is write-only by construction:
    `UserOut` does not list it, so no response can contain it.

    `min_length` is a placeholder, not a password policy. Django's own
    validators (AUTH_PASSWORD_VALIDATORS) are the real check and run in the
    view; this exists so the schema rejects an obviously-empty value before any
    of that.
    """

    password: str = Field(min_length=8, description="Plaintext; hashed before storage.")

    class Meta:
        model = User
        fields = ["username", "email"]


class UserUpdateIn(ModelSchema):
    """What a client may change afterwards — a smaller set than it may create.

    `username` is absent because identity changes are rarely a simple field
    update, and `password` is absent because changing one is its own operation
    with its own checks. That the create and update schemas differ is the
    normal case, and it is exactly what a single shared schema cannot express.

    Used with `PatchDict[UserUpdateIn]` at the endpoint, so the view receives
    only the keys the client actually sent. See docs/api.md — without it,
    "field omitted" and "field set to null" arrive identically.
    """

    class Meta:
        model = User
        fields = ["email", "first_name", "last_name"]
