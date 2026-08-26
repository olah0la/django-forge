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
