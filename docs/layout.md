# Project layout

Where code lives in this project, and why. The decision itself — and the options rejected — is
recorded in [ADR 0002](adr/0002-project-layout.md).

```
.
├── manage.py            Django's entry point
├── config/              project configuration — no business logic
│   ├── settings.py      settings (M3-02 splits this into a package)
│   ├── urls.py          root URLconf; mounts the API, defines no routes itself
│   ├── asgi.py          ASGI entrypoint (M3-04 configures it properly)
│   └── wsgi.py          WSGI entrypoint
├── apps/                every application lives here
│   └── core/            shared, cross-cutting code
├── docs/                documentation, including ADRs
└── tests/               test suite (arrives with the quality-gates phase)
```

## What belongs where

| Location | Belongs here | Does **not** belong here |
| --- | --- | --- |
| `config/` | Settings, root URLconf, ASGI/WSGI entrypoints | Models, views, business logic |
| `apps/<name>/` | One cohesive feature: its models, schemas, router, admin | Anything another app must import to function |
| `apps/core/` | Shared abstract models, mixins, common utilities | Feature-specific logic |
| `docs/` | Architecture notes and ADRs | Anything the code needs at runtime |

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

`runserver` served static files automatically in `DEBUG`; uvicorn does not. `config/asgi.py` wraps
the application in `ASGIStaticFilesHandler` **only when `DEBUG` is true**, so the admin stays styled
in development. Production is deliberately untouched — M6-03 decides how static files are served
there.

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

The difference is one flag. `make down` never passes `-v`, so the destructive form has to be typed
deliberately.

**Django does not use PostgreSQL yet.** Settings still point at SQLite; **M4-02** wires them to
`DATABASE_URL`. The entrypoint already waits for the database to accept queries before the
application starts.

## Adding an application

```bash
mkdir -p apps/<name>
docker compose exec app python manage.py startapp <name> apps/<name>
```

Then two steps that are easy to forget:

1. In `apps/<name>/apps.py`, set the **full dotted path**:

   ```python
   class BillingConfig(AppConfig):
       name = "apps.billing"     # not "billing"
   ```

   Django uses this string to locate the app's models, migrations, and templates. Leaving it as the
   bare name is the most common error with a nested `apps/` directory, and it fails confusingly.

2. Add it to `LOCAL_APPS` in the settings module.

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
