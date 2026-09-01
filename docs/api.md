# The API

The HTTP API is built with [Django Ninja](https://django-ninja.dev/), not Django REST Framework.
The two solve similar problems very differently, and **patterns copied from DRF tutorials generally
do not apply here** — there are no serializers, no viewsets, no DRF permission classes.

This document grows through M5. What exists today is the versioned instance, the
[router-per-app pattern](#routers-one-per-app), the [schema conventions](#schemas), and the
documentation page; [what is still owed](#what-m5-still-owes-this-document) is listed at the bottom.

```bash
make up
curl localhost:8000/api/v1/ping        # {"pong": true, "version": "1.0.0"}
open http://localhost:8000/api/v1/docs # interactive documentation
```

---

## One instance, mounted under a version prefix

`config/api.py` holds a single `NinjaAPI` instance. `config/urls.py` mounts it, in one line, at
`/api/v1/`. Each app contributes a router that attaches to that instance, and the instance
[holds no endpoints of its own](#routers-one-per-app).

It lives in `config/` rather than in an app because it is project wiring — the same category as the
root URLconf and the ASGI entrypoint. Endpoints belong to apps; the thing that mounts them does not.

### The prefix is there from the first endpoint, on purpose

Serving `/api/v1/ping` instead of `/ping` costs five characters and looks like ceremony while there
is exactly one version. It is the cheapest decision in this repository, and the most expensive one
to reverse: adding a prefix after clients have integrated breaks **every** consumer simultaneously,
and fixing it requires coordinating with all of them at once.

Rejected alongside it: **header-based versioning** — invisible in logs and browser URLs, and harder
to route at a proxy — and **no versioning**, which is fine right up until the first breaking change.

### Two different version numbers

This is the part that gets confused, so it is worth being explicit.

| | What it is | When it changes |
| --- | --- | --- |
| `v1` in the URL | The **contract boundary** | Only on a breaking change — a coordinated event |
| `settings.API_VERSION` | The **OpenAPI document version**, in `info.version` | Freely: `1.0.0 → 1.1.0` on any additive change |

Bumping the document version is routine and must never touch the URL. Bumping the URL version is a
project, described below.

Because of this, `urls_namespace` is **pinned** to `api-v1` in `config/api.py` rather than left at
Ninja's default. The default is `"api-" + version`, so releasing 1.1.0 would silently rename the URL
namespace and break every `reverse()` against it. The namespace identifies the *instance*, so it
follows the URL prefix, not the release. A test asserts this.

---

## Routers, one per app

Endpoints are **never** defined on the API instance. Each app declares a router in
`apps/<name>/api.py`, and `config/api.py` mounts it:

```python
# apps/billing/api.py
from ninja import Router

router = Router(tags=["billing"])          # tagged once, here — not per endpoint

@router.get("/invoices")
def list_invoices(request: HttpRequest) -> list[dict]:
    ...
```

```python
# config/api.py — the only file that knows an app has an API
from apps.billing.api import router as billing_router

ROUTERS: list[tuple[str, Router]] = [
    ("", core_router),          # the exception, below
    ("billing", billing_router),
]
```

That is `GET /api/v1/billing/invoices`.

**Why the split.** If every endpoint registered on the central instance, that module would grow
without bound and become a permanent merge-conflict site, because every feature branch would edit
the same file. Split this way, adding an endpoint to an existing app touches **one file** — the
app's own router — and adding an app costs one line in `ROUTERS`.

### The conventions

| | Convention | Why |
| --- | --- | --- |
| Location | `apps/<name>/api.py`, a module-level object named `router` | One place to look, in every app, forever |
| Prefix | The app's resource name, no slashes: `"billing"` | Ninja joins it to the mount point |
| Tags | Exactly one, named for the app, set on the `Router` | Groups the app in the docs page; cannot be forgotten per endpoint |
| Direction | `config/` imports apps; **an app never imports `config.api`** | The reverse is an import cycle that fails confusingly at startup |

An app that outgrows one module turns `api.py` into a package — `apps/<name>/api/` whose
`__init__.py` re-exports `router`. The import path `apps.<name>.api.router` does not change, so
`config/api.py` never has to know which shape an app is in.

Registration order in `ROUTERS` is the order the docs page lists the groups in. Keep it deliberate.

### `core` is the one router without a prefix

`apps/core/api.py` mounts at the API **root**: `/api/v1/ping`, not `/api/v1/core/ping`. Its
endpoints answer for the API as a whole rather than for a collection, and a "core" segment in the
URL would describe the codebase's layout rather than the resource.

**The exception is not extended.** A feature app mounted at the root puts its resources in the API's
root namespace, where the next app's resources collide with them — and unpicking that is a URL
change, which is a breaking change for every client.

### Adding an endpoint, adding an app

| Task | What you edit |
| --- | --- |
| An endpoint on an existing app | That app's `api.py`. Nothing else. |
| A new app's first endpoint | The app's `api.py`, plus one line in `ROUTERS` |

The second is also step 3 of [adding an application](layout.md#adding-an-application).

### Two errors Ninja raises, and what they actually mean

```
ConfigError: Router is already mounted to this API. When mounting the same router
multiple times, you must provide unique url_name_prefix for each mount.
```

The same router reached `add_router` twice on one instance — usually because an app registered
itself *and* was registered centrally. Keeping every mount in `ROUTERS`, rather than in
`AppConfig.ready()` hooks, is what makes this hard to do by accident.

```
ConfigError: Cannot add routers after URLs have been generated.
Add all routers before accessing api.urls
```

Ninja freezes the router list the first time `.urls` is read, which happens when Django loads the
URLconf. Anything mounting a router later — a lazy import, a plugin, a hook — is too late, and the
message names URL generation rather than the mount that caused it.

### `operationId` follows the module path

Ninja derives it as `module_name` with dots replaced, so `ping` in `apps/core/api.py` is
`apps_core_api_ping` — it was `config_api_ping` before M5-02 moved the endpoint.

**Moving a router module renames every operationId in it**, and generated clients bind to those
names: a method disappears and a new one appears, which reads to the consumer as a breaking change
even though no URL moved. This is not automated away here — a scheme that hides the module path
hides a real cost. Where a published client depends on a name, pin it:

```python
@router.get("/invoices", operation_id="list_invoices")
```

A collision — pinned or derived — is only *printed* by Ninja, never raised: the document then has
two operations claiming one name, and a generated client keeps whichever it read last. A test
asserts every id in the document is unique.

---

## Schemas

A schema is the contract: it validates what comes in, shapes what goes out, and generates the
OpenAPI document from the same declaration. Schemas live in `apps/<name>/schemas.py`, beside the
router that uses them — the package form (`apps/<name>/schemas/`) is the escape hatch when one
module is no longer enough.

`apps/core/schemas.py` is the worked example, over `django.contrib.auth.User`. **It has no
endpoints attached, deliberately** — authentication is still a documented stub (M5-07), and a
template shipping a working unauthenticated user API would be shipping a vulnerability to every
project forged from it. Copy the shape; add the endpoints once you have auth.
The round trip is exercised in `tests/test_schemas.py` against a test-only router.

### Input and output are always separate types

| Name | Direction | Holds |
| --- | --- | --- |
| `UserCreateIn` | request | What a client may set when creating |
| `UserUpdateIn` | request | The smaller set it may change afterwards |
| `UserOut` | response | What the API is willing to say back |

**Every input type ends `In`, every output type ends `Out`.** Direction in the name is what makes a
misuse visible at the call site: `response=UserCreateIn` reads as obviously wrong.

One shared schema is genuinely less code, which is why it happens under time pressure. It also does
two things silently: fields meant to be read-only become writable, and fields meant to be write-only
appear in responses. The example makes it concrete — `password` exists on `UserCreateIn` and
**cannot** appear in any response, because `UserOut` is a different type that does not list it. No
filtering code enforces that. The separation is the enforcement.

Create and update differ for the same reason: a client may set a username on creation and may not
change it afterwards, and a password change is its own operation with its own checks. A single
schema cannot express either.

### Response schemas are allow-lists

`Meta.fields` names every field, always. Ninja offers two shortcuts, and both are deny-lists in
disguise:

```python
fields = "__all__"          # every field — including the ones added next year
exclude = ["password"]      # every field EXCEPT today's known-dangerous ones
```

Measured against `User`: `fields = "__all__"` produces `password`, `is_superuser`, `is_staff`,
`last_login` and `user_permissions`. But the day it is written is not the problem. The problem is
the field somebody adds afterwards — an internal note, a soft-delete flag, a hashed token — which
appears in the API with **no diff for a reviewer to catch**, because the exposure happens in a file
nobody touched. An allow-list fails closed; a deny-list fails open.

`tests/test_schemas.py` rejects both shortcuts anywhere under `apps/`, and pins `UserOut`'s field
set exactly, so widening a response is a deliberate act that has to be typed out.

### Partial updates: `PatchDict`

```python
@router.patch("/{user_id}", response=UserOut)
def update_user(request, user_id: int, payload: PatchDict[UserUpdateIn]):
    for attribute, value in payload.items():
        setattr(user, attribute, value)
```

The view receives **only the keys the client actually sent**. That distinction is the entire reason
it is here: with an all-optional schema, a field the client never mentioned and a field explicitly
set to `null` arrive identically as `None`, and a loop like the one above blanks out every column
the client did not name. An empty `PATCH` body means "change nothing", and it should.

The cost is that the view receives a dict rather than a schema instance, which reads as a step
backwards until you know why. Note also that `PatchDict[UserUpdateIn]` publishes its component under
a **different name** — `UserUpdateInPatch` — and generated clients use the published one.

### Two costs of deriving schemas from models

`ModelSchema` keeps field types in step with the model, which is why it is used here. It is not
free:

- **The model's `help_text` becomes public API documentation.** Measured: `User.username`'s admin
  help text — "Required. 150 characters or fewer…" — is the `description` of that field in the
  published document. Help text written for the admin ships to API consumers verbatim.
- **mypy cannot see derived fields.** `payload.username` on a `ModelSchema` is an `attr-defined`
  error, because the fields are built by a metaclass at runtime. Use `payload.dict()` — which is
  also how the example separates the plaintext password from the columns it constructs the model
  from.

A model field's *type* change also moves the API contract without touching the schema. That is the
trade against hand-writing every output type, and the pinned field-set test keeps it visible.

---

## How a v2 would be introduced

Decided now, while nothing depends on it. Under pressure — a breaking change already required, a
consumer already waiting — this gets decided badly.

**v2 runs beside v1, not instead of it.**

1. A second `NinjaAPI` instance, with its own `urls_namespace` (`api-v2`), in `config/api.py`.
2. A second line in `config/urls.py`, added beside the first rather than replacing it:

   ```python
   path("api/v1/", api_v1.urls),
   path("api/v2/", api_v2.urls),
   ```

3. **Both live through a deprecation window.** Announce the window, give consumers a date, and
   measure v1 traffic rather than assuming it stopped.
4. v1 is removed only once its traffic is genuinely gone. A version nobody told you they were using
   is the normal case.

**What makes this work** is that the two are separate instances with separate namespaces — nothing
in the codebase assumes there is only one API.

**What breaks it** is sharing a `Router` object between the two instances. Ninja permits this — the
duplicate-mount check is per instance, so the same router mounted on `api_v1` and `api_v2` serves
happily from both (measured on 1.6.3; do not rely on the library to stop you). It defeats the point:
v2 exists precisely because the shape changed, and a shared router means every v1 change lands in v2
unreviewed. Give v2 its own routers, importing from v1 only where the behaviour is genuinely
identical.

Endpoints that did not change between versions still have to be *served* by both. Duplication
across a deprecation window is not a design flaw — it is the deprecation window doing its job.

---

## Interactive documentation

| Path | What it is |
| --- | --- |
| `/api/v1/docs` | Swagger UI — the browsable page, with a working "try it out" |
| `/api/v1/openapi.json` | The OpenAPI 3.1 document the page renders |

The schema is generated from the endpoint signatures, so it cannot drift from the implementation.
That is the whole argument for Django Ninja: **the type hints are the contract.**

### On in development, off in production

`API_DOCS_ENABLED` is `False` in `base.py`, so production inherits the safe value and nobody has to
remember anything. The development and test layers set it `True`.

The reason for the default is not that documentation is bad. It is that
`/api/v1/openapi.json` is a complete, machine-readable map of every endpoint, parameter and response
shape — which is exactly what someone enumerating an API wants, and which most internal APIs have no
reason to publish. When disabled, `docs_url` is `None` and the route does not exist: the path 404s
rather than rendering an empty page.

Publishing them is a normal choice for a public API, so production offers an override:

```bash
DJANGO_API_DOCS_ENABLED=true
```

Compare `SEED_ENABLED`, which has no environment switch at all. Seeding production is never correct;
publishing API docs sometimes is. The difference is deliberate — see `base.py`.

### Configurable metadata

Four variables, all optional, all with defaults in `base.py`:

| Variable | Becomes |
| --- | --- |
| `DJANGO_API_TITLE` | `info.title` |
| `DJANGO_API_VERSION` | `info.version` |
| `DJANGO_API_DESCRIPTION` | `info.description` |
| `DJANGO_API_DOCS_ENABLED` | Whether the docs route exists (production layer only) |

A template with a hard-coded API name is a template every adopter has to patch, so these are
environment-readable. They are project metadata rather than environment configuration, which is why
they live in `base.py` and not in a layer.

### Why `"ninja"` is in `INSTALLED_APPS`

**Not to route the API.** Everything answers correctly without it — which is exactly why someone
will eventually remove it as unnecessary.

Ninja checks `INSTALLED_APPS` when rendering the docs page. Present, it renders from the 7.9 MB of
Swagger UI assets bundled in the package, served through `django.contrib.staticfiles`. Absent, it
falls back to a template that loads `swagger-ui-bundle.js` from `cdn.jsdelivr.net` — so the page
breaks with no network, and every developer opening it makes a request to a third party.

The consequence to know: in production the docs page needs `collectstatic` to have run, or it
renders without its assets. `TODO(M6-03)` owns making that part of the image build.

---

## What M5 still owes this document

Each of these arrives with its issue, and this file is where it gets written down:

- **M5-04** — the pagination strategy applied to every list endpoint
- **M5-05** — the single error envelope, through centralised exception handlers
- **M5-07** — the authentication seam, deliberately a documented stub rather than an implementation

---

## See also

- [layout.md](layout.md#what-belongs-where) — why the instance is in `config/` and routers are not
- [Django Ninja documentation](https://django-ninja.dev/)
- [OpenAPI 3.1 specification](https://spec.openapis.org/oas/v3.1.0)
