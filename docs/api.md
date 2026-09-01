# The API

The HTTP API is built with [Django Ninja](https://django-ninja.dev/), not Django REST Framework.
The two solve similar problems very differently, and **patterns copied from DRF tutorials generally
do not apply here** — there are no serializers, no viewsets, no DRF permission classes.

This document grows through M5. What exists today is the versioned instance and its documentation
page; [what is still owed](#what-m5-still-owes-this-document) is listed at the bottom.

```bash
make up
curl localhost:8000/api/v1/ping        # {"pong": true, "version": "1.0.0"}
open http://localhost:8000/api/v1/docs # interactive documentation
```

---

## One instance, mounted under a version prefix

`config/api.py` holds a single `NinjaAPI` instance. `config/urls.py` mounts it, in one line, at
`/api/v1/`. From M5-02 each app contributes a router that attaches to that instance; the instance
itself will hold no endpoints of its own.

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

**What breaks it** is sharing a `Router` object between the two instances. Ninja raises a
configuration error when the same router is added twice, and reusing routers across versions also
defeats the point: v2 exists precisely because the shape changed. Give v2 its own routers, importing
from v1 only where the behaviour is genuinely identical.

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

- **M5-02** — the router-per-app pattern; the `/ping` endpoint currently defined on the instance
  moves to `apps/core/api.py`, and the central instance stops holding endpoints
- **M5-03** — request and response schema conventions, and why input and output schemas stay
  separate
- **M5-04** — the pagination strategy applied to every list endpoint
- **M5-05** — the single error envelope, through centralised exception handlers
- **M5-07** — the authentication seam, deliberately a documented stub rather than an implementation

---

## See also

- [layout.md](layout.md#what-belongs-where) — why the instance is in `config/` and routers are not
- [Django Ninja documentation](https://django-ninja.dev/)
- [OpenAPI 3.1 specification](https://spec.openapis.org/oas/v3.1.0)
