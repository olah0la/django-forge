# 🔨 django-forge

**A production-minded starting point for dockerized Django backend projects.**

Clone it, run one bootstrap command, and begin with the container setup, configuration layering,
database foundations, and API structure already solved — instead of rebuilding them from scratch at
the start of every project.

---

## 🚧 Project status: in progress

> **The stack runs; the application layer is still being built.**
>
> `make up` starts Django on <http://localhost:8000>, backed by PostgreSQL. What exists today is a
> governed repository with locked dependencies (**M1**), a containerized stack with two profiles
> (**M2**), a Django project with layered 12-factor settings (**M3**), and the persistence layer —
> PostgreSQL, a migration workflow, and shared base models (**M4**, in progress). Still ahead: the API layer (**M5**), operational readiness (**M6**), and
> the template tooling that makes this consumable as a starting point (**M7**) — a backlog of 40
> issues tracked in [GitHub Milestones](https://github.com/olah0la/django-forge/milestones) and
> [Issues](https://github.com/olah0la/django-forge/issues).
>
> Sections describing work that has not landed are marked 🔜.

If you are looking for a first task, see
[Your first contribution](#-your-first-contribution).

---

## 🎯 What this project is

Most teams starting a Django backend spend their first days on the same problems: writing a
Dockerfile, deciding how settings should differ between environments, wiring PostgreSQL, and
agreeing on how the API should be structured. Those decisions get made in a hurry, differently each
time, and the mistakes are inherited by the project for years.

django-forge solves them **once**, deliberately, and documents the reasoning — so that every project
forged from it starts from a considered baseline rather than a blank directory.

### What that means in practice

This is a **template repository**, not an application. That distinction drives nearly every decision
in the roadmap: a choice that is merely convenient for one application can be actively wrong for a
template that many applications will inherit. Where a decision is a genuine trade-off, the intent is
to record it as an Architecture Decision Record rather than leave the next team guessing.

---

## 🧰 Planned stack

| Technology | Role | Why this choice |
| --- | --- | --- |
| **Python 3.12** | Language | Pinned to a single minor version so local and container environments agree; declared in `.python-version` and `pyproject.toml` |
| **Django 5.2 LTS** | Web framework | Mature, batteries-included, and the ORM plus migration system removes a large class of work you would otherwise hand-roll. The LTS line receives security fixes until roughly April 2028, so derived projects are not forced into a framework upgrade within the year |
| **Django Ninja** | API layer | Builds APIs from standard Python type hints, generating request validation and OpenAPI documentation automatically — the type hints *are* the contract |
| **uv** | Dependency management | Fast, standards-based (PEP 621 / PEP 735), and `uv.lock` pins exact versions and hashes so installs are reproducible — see [ADR 0001](docs/adr/0001-dependency-manager.md) |
| **PostgreSQL** | Database | The production-grade default; developing against the same engine you deploy to avoids a whole category of production-only bugs |
| **Docker + Compose** | Runtime & local stack | Bundles the app with the exact runtime it needs, so behaviour is identical across machines and in production |
| **ASGI** (Gunicorn + Uvicorn workers) | Application server | Django Ninja supports async endpoints; committing to ASGI now keeps that option open at no cost today |

> ℹ️ **Note on Django Ninja.** This project does **not** use Django REST Framework. The two solve
> similar problems very differently, and patterns copied from DRF tutorials will generally not
> apply here.

---

## 🗺️ Roadmap

Seven milestones, ordered by dependency rather than preference. Each assumes the machinery built by
the ones before it, so reordering them creates rework.

| # | Milestone | Goal | Status |
| --- | --- | --- | --- |
| **M1** | Foundation & Developer Environment | A reproducible, well-governed repository | ✅ Complete |
| **M2** | Containerization | The project builds and runs in Docker, identically for everyone | ✅ Complete |
| **M3** | Django Project Scaffold & Configuration | A Django project with 12-factor configuration | ✅ Complete |
| **M4** | Persistence Layer | PostgreSQL, migrations, and shared model foundations | 🚧 In progress |
| **M5** | API Layer (Django Ninja) | A versioned, documented, consistently-erroring HTTP API | 🔜 Not started |
| **M6** | Operational Readiness | The service is safe to run under a process manager or orchestrator | 🔜 Not started |
| **M7** | Template Consumability | The repository can actually be used as a starting point | 🔜 Not started |

Live status, full scope, and exit criteria for each milestone live in
[GitHub Milestones](https://github.com/olah0la/django-forge/milestones). Individual work items are
in [Issues](https://github.com/olah0la/django-forge/issues).

### 🚫 Deliberately out of scope for now

These are recognised as necessary for a production backend and are planned as a **second phase**,
once the core platform is stable. They are listed so their absence reads as a decision, not an
oversight.

| Area | Why deferred |
| --- | --- |
| **Asynchronous processing** (Redis, Celery) | Depends on a stable settings and container layer; building it earlier means rebuilding it after M3 |
| **Authentication & authorization** (JWT, permissions) | M5 delivers the extension point deliberately, so this can be added later without reworking the API layer |
| **CI/CD & quality gates** (Actions, tests, linting) | Valuable early, but this phase was scoped to the core platform; expected as the immediate next phase |
| **Observability** (error tracking, tracing, metrics) | Builds directly on the structured logging foundation from M6 |

> ⚠️ **Deferring automated testing does not mean deferring verification.** Until a test suite
> exists, every milestone's exit criteria must still be *demonstrated* by hand before it closes.
> "It should work" is not evidence.

---

## 🚀 Getting started 🔜

> **Not yet available.** This section describes the intended experience, delivered by **M7 —
> Template Consumability**. Until then there is nothing to run.

Once M1–M7 are complete, starting a new project will look like this:

```bash
git clone https://github.com/olah0la/django-forge.git my-project
cd my-project
make bootstrap          # rename the project, generate a fresh secret key
cp .env.example .env    # fill in local values
make up                 # start the full stack
```

**Planned prerequisites:** Docker and Docker Compose. Everything else runs inside containers, so no
local Python installation or virtual environment is required.

### ✅ What works today

Dependency management is in place (M1-02), so the Python environment can already be installed.
Install [uv](https://docs.astral.sh/uv/), then:

```bash
make help        # list every available task — start here
make install     # create .venv and install runtime + dev dependencies
make check       # lint, type-check and test
```

The container stack runs too (M2-01 through M2-04). It describes two stacks in one
`docker-compose.yml`, selected by Compose *profiles*:

```bash
make build       # build the image
make up          # start the development stack   (host port 8000)
make up-prod     # start the production-like one (host port 8001)
make ps          # what is running, and whether it is healthy
make shell       # a shell inside the app container — `whoami` returns `app`, not root
make down        # stop everything, both profiles
```

The Django project now runs: `make up` serves it on <http://localhost:8000>. Where code lives is
documented in [docs/layout.md](docs/layout.md) — `config/` for project configuration, `apps/` for
applications, `apps/core/` for shared code.

**PostgreSQL 17.6 is the database in development as well as production** (M4-01, M4-02), so local
behaviour matches deployed behaviour rather than diverging through SQLite:

```bash
make migrate            # apply migrations
make makemigrations     # generate them from model changes
make migrations-check   # fail if a model change has no migration — run before pushing
make db-shell           # a psql session against the development database
make test-db            # run the test suite against real PostgreSQL, in the container
make seed               # load development seed data — a superuser and permission groups
```

`make seed` is idempotent and **development-only**: it refuses unless `settings.SEED_ENABLED` is
true, which only the development and test layers set — in code, never from `.env`, since that file
is read by both Compose profiles.

**Shared abstract base models** (M4-04) give every model a UUIDv7 primary key and automatic
created/updated timestamps:

```python
from apps.core.models import BaseModel

class Invoice(BaseModel):        # gets id, created_at, updated_at
    reference = models.CharField(max_length=32, unique=True)
```

UUIDv7 rather than a sequential integer because an integer in a URL leaks how many records exist and
invites enumeration; v7 rather than v4 because its timestamp prefix keeps inserts clustered in the
index — measured at 3 index pages read against 1,157 for the same workload.
[docs/models.md](docs/models.md) has the numbers, the costs, and the soft-deletion pattern that is
deliberately documented rather than shipped.

Migrations are deliberately **not** applied at container startup — during a rolling deploy every
replica would race to apply the same one. [docs/migrations.md](docs/migrations.md) covers the
workflow, the branch-conflict procedure, and the operations that turn a schema change into an
outage; the [review checklist](CONTRIBUTING.md#migration-review-checklist) is the short form.

The development stack **mounts your working copy into the container**, so an edit on your machine
takes effect immediately with no rebuild. The production-like stack deliberately does not — it runs
the image exactly as built. See [CONTRIBUTING.md](CONTRIBUTING.md) for the details.

Every container reports a **health status**, so `make ps` shows `Up 20 seconds (healthy)` rather
than just `Up`. The database's check confirms it accepts connections, and the app waits on that
`condition: service_healthy` rather than a bare dependency — which removes a whole class of
intermittent startup failures. The app's own check proves its runtime is intact; it does *not* yet
prove it can serve traffic, because **M6-01** replaces the probe with a request to the readiness
endpoint.

Development is the default profile, so a bare `docker compose up` starts it with no flag. The
production-like profile is opt-in and must name its service:
`docker compose --profile prod up app-prod`. Both run the same image; the second runs it the way a
deployment would, which is what makes a production-only problem reproducible locally.

> **Linux users whose `id -u` is not 1000:** export `APP_UID=$(id -u)` and `APP_GID=$(id -g)`
> before building. A bind mount keeps the host's numeric owner, so a container user with a
> different UID cannot write the files it mounted — this bites on Linux and not on macOS.

The development stack runs Django under uvicorn with auto-reload; the production-like one runs
the same image the way a deployment would. **M6-02** still owns putting gunicorn in front of it.

Targets tagged for milestones that have not landed are listed but not usable — running one tells you
which file is missing and which issue delivers it, rather than failing with a raw error. The
underlying commands are plain `uv`:

```bash
uv sync                      # what `make install` runs
uv sync --frozen --no-dev    # runtime only — what production images will install
uv lock                      # re-resolve after editing dependencies in pyproject.toml
```

`uv` reads the pinned interpreter from `.python-version` and provisions Python 3.12 itself, so no
local Python installation is needed. **`uv.lock` is committed and must never be edited by hand** —
it is what makes every install resolve to identical versions.

---

## 🤝 Your first contribution

New to the project? Start with a
[`good first issue`](https://github.com/olah0la/django-forge/issues?q=is%3Aissue+is%3Aopen+label%3A%22good+first+issue%22)
— these are self-contained and chosen so you can finish one without holding the whole system in
your head.

**[CONTRIBUTING.md](CONTRIBUTING.md) is the full guide**: setup, branching, commit conventions, what
reviewers check, and the Definition of Done. It is short by design.

> 💡 **Asking questions is not a failure mode.** A question asked early costs minutes; a wrong
> assumption discovered in review costs a day. If something in an issue or in this README is
> unclear, that is worth raising — an issue you cannot understand is a defect in the issue.

---

## 📖 Concepts glossary

Terms used across the roadmap and issues. If you have not built and deployed a containerized
service before, these are the ones worth knowing first — none of them are as complicated as they
sound.

| Term | What it means |
| --- | --- |
| **Lock file** | A file recording the exact resolved version of every dependency, including the dependencies *of* your dependencies. It is what makes an install today and an install in six months produce the same environment. |
| **Multi-stage build** | A Dockerfile technique using one image to compile and install dependencies, then copying only the finished result into a smaller final image. Compilers and build tools never reach production, cutting both image size and attack surface. |
| **12-factor configuration** | The principle that anything differing between environments — credentials, hostnames, log levels — comes from *environment variables*, not from code. It is what lets the exact same built image be promoted from staging to production. |
| **ASGI / WSGI** | The interfaces between a web server and a Python application. WSGI handles one request per worker at a time; ASGI additionally supports asynchronous views and long-lived connections. |
| **Migration** | A versioned, ordered description of a database schema change. Django generates them from your model changes. They are also the most common cause of serious production incidents — an operation that is instant on 50 local rows can lock a 50-million-row table. |
| **Liveness vs. readiness** | Two different questions an orchestrator asks. *Liveness*: "is this process healthy, or should I restart it?" *Readiness*: "should I send it traffic right now?" A container still opening its database connections is alive but not ready — conflating the two causes outages. The `HEALTHCHECK` in the Dockerfile is the *container-level* answer; **M6-01** adds the *application-level* one as HTTP endpoints. |
| **Error envelope** | A single documented JSON shape used for every error the API returns. Without one, clients must handle a different shape per error type, and will get at least one of them wrong. |
| **Correlation ID** | A unique value attached to every log line produced while handling one request, so those lines can be reassembled from logs interleaved across many concurrent requests. |
| **Graceful shutdown** | Finishing in-flight requests when the platform sends `SIGTERM`, instead of dropping them. Containers are stopped on every deploy, so an app that ignores `SIGTERM` drops requests on every release. |
| **ADR** | *Architecture Decision Record* — a short document capturing one decision: the context, the options considered, the choice, and the consequences. It is the difference between "this is how it is" and "this is why it is, and when you should choose differently." |

---

## 📄 License

Released under the **MIT License** — see [LICENSE](LICENSE).

You may use, modify, and redistribute this template, including commercially, provided the copyright
notice is kept. Projects forged from it carry no obligation to be open source.
