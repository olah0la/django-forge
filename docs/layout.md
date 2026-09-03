# Project layout

Where code lives in this project, and why. The decision itself — and the options rejected — is
recorded in [ADR 0002](adr/0002-project-layout.md).

```
.
├── manage.py            Django's entry point
├── config/              project configuration — no business logic
│   ├── settings.py      settings (M3-02 splits this into a package)
│   ├── urls.py          root URLconf; mounts the API and the health probes
│   ├── logging.py       filters referenced by the LOGGING dict
│   ├── asgi.py          ASGI entrypoint (M3-04 configures it properly)
│   └── wsgi.py          WSGI entrypoint
├── apps/                every application lives here
│   └── core/            shared, cross-cutting code
│       ├── api.py       the app's django-ninja router — one per app
│       ├── health.py    liveness and readiness views
│       └── models.py    shared abstract base models
├── docs/                documentation, including ADRs
└── tests/               test suite (arrives with the quality-gates phase)
```

## What belongs where

| Location | Belongs here | Does **not** belong here |
| --- | --- | --- |
| `config/` | Settings, root URLconf, the API instance (`api.py`), ASGI/WSGI entrypoints | Models, views, business logic, endpoint definitions |
| `apps/<name>/` | One cohesive feature: its models, schemas, router, admin | Anything another app must import to function |
| `apps/core/` | Shared abstract models, mixins, common utilities | Feature-specific logic |
| `docs/` | Architecture notes and ADRs | Anything the code needs at runtime |

`config/api.py` holds the single `NinjaAPI` instance, mounted at `/api/v1/` by `config/urls.py`.
The instance is project wiring and lives in `config/`; the routers that attach to it belong to the
apps they serve. It **defines no endpoints itself** — it is a mounting table — and the dependency
runs one way: `config/` imports app routers, and an app never imports `config.api`.
**Read [api.md](api.md) before changing the URL prefix** — it is the one decision here that cannot
be revised once a client exists.

### The three exceptions in the root URLconf

`config/urls.py` carries exactly two things that are not the API mount: the admin, and the health
probes (`/healthz`, `/readyz`). Both are project wiring rather than application endpoints, which is
why they sit beside the API mount instead of inside a router.

The views themselves live in `apps/core/health.py`, because they are code and code belongs in an
app — but their **URLs** are deliberately outside `/api/v1/`. A probe URL is an infrastructure
contract, held by Dockerfiles and orchestrator manifests; the API prefix is a contract boundary for
API clients, and a future v2 must not be able to move a path that deployments depend on. See
[ops.md](ops.md).

This is not an invitation to add a third. Feature endpoints go in a router.

## Settings layers

`config/settings/` is a package, not a module. One layer is active at a time, chosen by
`DJANGO_SETTINGS_MODULE`:

| Module | Used by | Character |
| --- | --- | --- |
| `config.settings.base` | nothing directly | Shared configuration only |
| `config.settings.development` | `make up` | `DEBUG` on, permissive hosts, console email |
| `config.settings.production` | `make up-prod` | `DEBUG` off and unopenable, hosts from the environment |
| `config.settings.test` | pytest | In-memory database, fast password hasher |

**Which value wins.** Each environment layer does `from .base import *` and then overrides. That is
exactly one hop: to answer "where does this setting come from?", look at your layer, then at `base`.
If a change ever requires a third file to answer that question, the layering has gone wrong.

**What goes where.** If a value is the same everywhere, it belongs in `base`. If it differs, it
belongs in each layer that needs it — never behind an `if DEBUG:` branch in `base`, which is how a
development convenience silently reaches production.

**There is no default layer.** `DJANGO_SETTINGS_MODULE` must be set explicitly; running without it
fails with a message listing the valid modules. This is deliberate — a default means a production
process with the variable accidentally unset boots on development settings, with debug on. Both
Compose profiles set it, so containers need no thought.

```bash
DJANGO_SETTINGS_MODULE=config.settings.development python manage.py check
```

**Production refuses to enable `DEBUG`.** Setting `DJANGO_DEBUG=1` against the production layer
raises `ImproperlyConfigured` rather than being quietly ignored, so whoever set it learns why it did
not take effect.

## Environment variables

Values are read through a **typed** reader (`env` in `config/settings/base.py`), not raw
`os.environ`. Environment variables are always strings, and reading them raw produces the classic
bug: `DJANGO_DEBUG="False"` is a non-empty string and therefore truthy, so debug mode is silently on
in production.

```python
env.bool("DJANGO_DEBUG", default=True)      # "False" -> False
env.list("DJANGO_ALLOWED_HOSTS", default=[])
require("DJANGO_SECRET_KEY")                # empty or unset -> startup fails
```

`.env.example` documents every recognised variable. Copy it to `.env` — which is git-ignored — and
edit. **You do not need one**: the development layer runs with nothing set.

**What is required, and where.** Only the production layer requires anything: `DJANGO_SECRET_KEY`
and `DJANGO_ALLOWED_HOSTS`. Both use `require()`, which treats an **empty** value as missing —
`DJANGO_ALLOWED_HOSTS=` would otherwise boot with no allowed hosts and reject every request with no
explanation.

**Development generates a `SECRET_KEY`** when none is supplied, so no insecure key is committed
anywhere. The cost: it changes on each restart, so logins do not survive a reload. Set
`DJANGO_SECRET_KEY` in `.env` to pin one.

## ASGI, and what it costs

Both profiles serve `config/asgi.py` through **uvicorn** — development with `--reload`, production
without. Django's `runserver` is WSGI-only, so using it would have left the ASGI entrypoint
unexercised until the first async endpoint.

ASGI was chosen because Django Ninja supports async endpoints, and reversing the choice later would
touch the server, the container, and every endpoint written in between. **Nothing forces a view to
be async** — the interface only keeps the option open.

### The constraint to know before writing an async view

Django's ORM is **synchronous**. Calling it from an `async def` view raises `SynchronousOnlyOperation`:

```python
async def bad(request):
    return JsonResponse({"n": User.objects.count()})
    # SynchronousOnlyOperation: You cannot call this from an async context
    #                           - use a thread or sync_to_async.
```

Use the async ORM methods, or push synchronous work to a thread:

```python
async def good(request):
    return JsonResponse({"n": await User.objects.acount()})

from asgiref.sync import sync_to_async
result = await sync_to_async(some_sync_function)()
```

Async Django is not "Django but faster". An async view that calls synchronous code without wrapping
it either raises, or silently blocks the event loop — which is worse, because it looks like it works.

### Static files

`runserver` served static files automatically in `DEBUG`; uvicorn does not. **WhiteNoise middleware**
serves them instead, in every settings layer, so the mechanism exercised on a laptop is the one that
runs in production (M6-03). `config/asgi.py` no longer wraps the application in
`ASGIStaticFilesHandler` — that wrapper was a debug convenience with no caching, compression or
content hashing, and keeping it would have meant two mechanisms with only one of them ever tested.

Development and the test layer resolve through the staticfiles finders (`WHITENOISE_USE_FINDERS`),
so neither needs `collectstatic` to have run. Production reads the collected, hashed, pre-compressed
tree that the image build produced. See [serving.md](serving.md).

## Production hardening

`make audit` runs Django's deploy checks against the production layer and a secret scan over the
full git history. Both must be clean before a release.

| Setting | Default | Why |
| --- | --- | --- |
| `SESSION_COOKIE_SECURE` / `CSRF_COOKIE_SECURE` | `True` | Unconditional — no production reason to send these in plaintext |
| `SECURE_SSL_REDIRECT` | `True` | Set false where TLS terminates upstream and the balancer already redirects |
| `SECURE_PROXY_SSL_HEADER` | **off** | Opt in with `DJANGO_TRUST_PROXY_SSL_HEADER` |
| `SECURE_HSTS_SECONDS` | `3600` | One hour, not one year — see below |

**Why the proxy header is opt-in.** Trusting `X-Forwarded-Proto` unconditionally is itself a
vulnerability: if the application is ever reachable directly, a client can send the header
themselves and convince Django a plaintext request was secure. Enable it only behind a proxy that
**overwrites** the header on every request.

**Why HSTS starts at one hour.** Browsers cache the policy. Advertising a year before HTTPS is
proven stable can make the site unreachable for a year, and you cannot clear it from users'
browsers. Ramp it once HTTPS is stable: `3600` → `86400` → `31536000`, then consider preload.

**One check is waived:** `security.W021` (HSTS preload). Preload is a commitment to a browser-vendor
list that is slow to leave, and a template must not make it on a derived project's behalf. The
justification is written out in `config/settings/production.py`; enabling it is one variable.

### Layer-specific values do not belong in `.env`

A `.env` is read by **both** Compose profiles. Two variables are therefore commented out in
`.env.example`, and each Compose service pins its own settings module:

- `DJANGO_DEBUG` — a truthy value stops the production profile from starting at all.
- `DJANGO_SETTINGS_MODULE` — a development value here made the production-like profile silently run
  development settings: debug on, no HSTS, no secure cookies, while still reporting healthy.

## The database

PostgreSQL **17.6** runs as a Compose service. Development uses the same engine as production
because SQLite differs in transaction behaviour, type handling and constraint enforcement — a class
of bugs that would otherwise appear only after deploy.

```bash
make up          # starts the app and the database
make db-shell    # a psql session inside the container
```

**Why the minor version is pinned.** Tracking a major tag such as `postgres:17` means an unexpected
major upgrade can leave the existing data directory unreadable: the container starts, refuses to
read its own data, and the local database is effectively gone. Keep the pin in step with whatever
production runs.

**Why Debian and not Alpine.** Alpine would save around 150 MB, but musl's collation differs from
glibc, so text ordering and some index behaviour would not match managed PostgreSQL services — which
is precisely the local-versus-production divergence this service exists to remove.

**The port is not published.** The app reaches the database over the Compose network and `make
db-shell` runs inside the container, so nothing needs a host port — and publishing 5432 collides
with any locally-installed PostgreSQL. For a GUI client, add a git-ignored
`docker-compose.override.yml`:

```yaml
services:
  db:
    ports: ["5433:5432"]
```

### ⚠️ Which command destroys your data

| Command | Effect |
| --- | --- |
| `make down` | Stops containers, **keeps** the database |
| `docker compose down -v` | **Deletes** the volume — unrecoverable, no prompt |
| `make db-restore FILE=…` | **Drops** the database and rebuilds it from a dump — prompts first |

The difference between the first two is one flag. `make down` never passes `-v`, so the destructive
form has to be typed deliberately; `make db-restore` asks for the database name back for the same
reason.

Only the third has a way back, and only if you took a dump first — see
[backups.md](backups.md), which is also emphatic that this is a local convenience and not a backup
strategy.

### Connecting to it

Django reads a single `DATABASE_URL` rather than five separate variables: most platforms hand you a
URL, and five variables are five chances to configure a partially-wrong database.

**It is required, with no fallback.** Falling back to SQLite would let host-side commands run
against a different engine than the container — silently. Django commands therefore run through the
container: `make migrate`, `make django-shell`, `make db-shell`. Running `manage.py` directly on the
host fails with a message saying exactly that.

### Connection reuse, and the arithmetic that bounds it

`CONN_MAX_AGE` (default **60s**) keeps a connection open for reuse instead of opening one per
request. `CONN_HEALTH_CHECKS` turns on with it: a pooled connection can be killed by a database
restart, and without a check the next request to reuse it fails with an unexplained `InterfaceError`.

> **total connections ≈ workers × threads per worker**

Not workers alone — this is the part that surprises people. Under ASGI, sync views run in a
threadpool and Django connections are **thread-local**, so every busy thread holds its own.

Measured on this stack, one uvicorn process, `CONN_MAX_AGE=60`:

| Load | Connections held |
| --- | --- |
| 40 sequential requests | 9 |
| 20 concurrent requests | **20** |

PostgreSQL's default `max_connections` is **100**. So a modest 4 workers × 16 threads is 64
connections before any other client connects — and exceeding the limit refuses new connections
outright, which is the most common way a worker-count increase causes an outage.

Levers, in order of preference:

1. Lower `DJANGO_CONN_MAX_AGE` — idle connections are released sooner.
2. Cap the threadpool (`ASGI_THREADS`) and worker count together (**M6-02** sets these).
3. **PgBouncer**, at scale. Not a larger `max_connections`: each connection costs memory
   server-side, so raising the limit moves the failure rather than removing it.

### Migrations

Generating, reviewing and applying them — and the operations that turn a schema change into an
outage — are covered in **[migrations.md](migrations.md)**. The short version reviewers use in a
pull request is the [checklist](../CONTRIBUTING.md#migration-review-checklist) in `CONTRIBUTING.md`.

Migrations are deliberately **not** run at container startup: during a rolling deploy every replica
would race to apply the same migration, and a long one blocks startup past the health timeout. Run
them explicitly with `make migrate`, and check for drift with `make migrations-check`.

## Adding an application

```bash
mkdir -p apps/<name>
docker compose exec app python manage.py startapp <name> apps/<name>
```

Then three steps that are easy to forget:

1. In `apps/<name>/apps.py`, set the **full dotted path**:

   ```python
   class BillingConfig(AppConfig):
       name = "apps.billing"     # not "billing"
   ```

   Django uses this string to locate the app's models, migrations, and templates. Leaving it as the
   bare name is the most common error with a nested `apps/` directory, and it fails confusingly.

2. Add it to `LOCAL_APPS` in the settings module.

3. If the app serves endpoints, create `apps/<name>/api.py` with a `router`, and add one line to
   `ROUTERS` in `config/api.py`:

   ```python
   ROUTERS: list[tuple[str, Router]] = [
       ("", core_router),
       ("billing", billing_router),      # -> /api/v1/billing/...
   ]
   ```

   The conventions — prefix, tags, and why `core` is the one router without a prefix — are in
   [api.md](api.md#routers-one-per-app).

## Models

Shared abstract base models live in `apps/core/models.py`: a UUIDv7 primary key and automatic
created/updated timestamps, which most models in a derived project should inherit via `BaseModel`.

**[models.md](models.md)** covers the worked example, the measured reasoning behind the primary-key
choice, the timestamp write that silently skips `updated_at`, and the soft-deletion pattern — which
is documented deliberately rather than shipped.

## Logging

`apps/core/logging.py` holds the formatters, the correlation filter and the redaction helpers;
`apps/core/middleware.py` holds the middleware that gives each request its identifier and logs it.
`config/settings/base.py` assembles them with `build_logging()`, which each settings layer calls
with the format that layer wants.

Two constraints that are not obvious from the files:

- **`apps/core/logging.py` may import nothing but the standard library.** Django configures logging
  *before* the app registry is populated, so that module is imported while `apps.populate()` has not
  run — an import that reaches a model fails at startup.
- **`RequestIDMiddleware` belongs first in `MIDDLEWARE`.** Middleware wraps in list order, so
  anything above it logs uncorrelated lines.

**[logging.md](logging.md)** covers the two formats, the `X-Request-ID` contract, and the four
controls that keep passwords and tokens out of the log.

## Why `apps/core/` is an app, not a plain package

It could have been a loose `shared/` package, but shared code here will include **abstract base
models** (M4-04). Anything owning models needs to be an installed app so Django's machinery finds
its migrations and its app registry entry. Making it an app now avoids converting it later.

`apps/core/` is also the worked example of the convention: it shows the file layout, the `AppConfig`
naming, and the registration pattern that every other app follows.

## Why not a `src/` layout

`src/` is good modern Python packaging practice, but it needs the project installed or `PYTHONPATH`
set. The Docker build deliberately installs dependencies with `--no-install-project`, so a `src/`
layout would require extra machinery purely to make imports resolve — and the acceptance criterion
for this structure is that imports work with **no path manipulation at runtime**. See ADR 0002.
