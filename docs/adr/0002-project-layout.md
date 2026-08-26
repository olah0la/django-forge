# 2. Project and application layout

- **Status:** Accepted
- **Date:** 2026-08-25
- **Relates to:** M3-01

## Context

`django-admin startproject` produces a layout designed for a tutorial. Projects that keep it tend to
grow one sprawling app that holds everything, because there is no obvious place to put the second
one. Deciding the structure before there is code costs an hour; deciding it after twenty modules
exist costs a refactor that touches every import in the codebase.

This is a template, so the choice is inherited by every project forged from it — and those teams
will not be present for this discussion.

Requirements:

1. Project configuration separated from application code.
2. An obvious, uniform home for each new application.
3. A defined place for shared, cross-cutting code.
4. Imports that work with **no path manipulation at runtime**.

## Options considered

### A. `config/` + `apps/` + `manage.py` at the root

Configuration in `config/`, every application under `apps/`, entry point at the root. Imports are
plain (`apps.core`, `config.settings`) and resolve because the working directory is the project root.

Against: `apps.core` is slightly longer to type than `core`, and each app's `AppConfig.name` must use
the full dotted path or Django will not find it.

### B. `src/` layout

Everything beneath `src/`. The modern Python packaging convention; prevents accidentally importing
from the working directory instead of the installed package.

Against, and decisively: it requires the project to be installed, or `PYTHONPATH` to be set. The
Docker build installs dependencies with `uv sync --no-install-project` precisely so that dependency
layers cache independently of source. Adopting `src/` would mean either installing the project into
the image (losing that caching property) or setting `PYTHONPATH` — which is exactly the runtime path
manipulation requirement 4 rules out.

### C. `config/` + apps as top-level packages

Configuration in `config/`, but each app its own top-level directory — closest to what `startapp`
produces.

Against: the repository root accumulates one directory per app, mixing application code with
tooling files (`Makefile`, `Dockerfile`, `pyproject.toml`). At twenty apps the root becomes hard to
scan, and there is no single place to point someone at to answer "where does the code live?".

## Decision

**Option A.** Configuration in `config/`, applications under `apps/`, `manage.py` at the root, and
shared code in `apps/core/` as a genuine Django app.

Requirement 4 was the deciding factor. Option B is defensible in most Python projects, but it
conflicts directly with a Docker build decision already made and verified in M2-01 — and that
build's caching behaviour is worth more to this template than `src/`'s import-shadowing protection,
which matters far less for an application than for a distributed library.

## Consequences

**Positive**

- One obvious place for every kind of code; "where does this go?" has a documented answer.
- The repository root stays scannable no matter how many apps exist.
- Imports need no `PYTHONPATH`, no `sys.path` edits, and no install step.
- `apps/core/` can own abstract base models (M4-04) because it is a real app.

**Negative**

- `AppConfig.name` must be the full dotted path (`apps.billing`, not `billing`). This is the most
  common mistake with a nested layout, so `docs/layout.md` calls it out explicitly.
- The template does not get `src/`-layout import shadowing protection. Accepted: this is an
  application, not a library published to an index.

**Neutral**

- `config` is a generic name. That is deliberate — a project-specific name would have to be renamed
  by the bootstrap step (M7-01), and every derived project would then differ.
