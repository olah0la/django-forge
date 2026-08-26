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
