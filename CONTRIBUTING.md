# Contributing to django-forge

This guide covers how work is set up, branched, committed, and reviewed here. It is deliberately
short — read it once and refer back as needed.

If anything below is unclear or turns out to be wrong, say so. A guide nobody trusts is worse than
no guide.

---

## Before you start

| Tool | Why | Install |
| --- | --- | --- |
| **git** | Version control | Preinstalled on most systems |
| **uv** | Dependency management | https://docs.astral.sh/uv/getting-started/installation/ |
| **make** | Task runner | Preinstalled on macOS and Linux |

You do **not** need a local Python installation — `uv` reads `.python-version` and provisions
Python 3.12 itself.

Docker and Docker Compose **are** prerequisites now: the container stack landed with M2. `make up`
starts the development stack and `make up-prod` the production-like one; `make down` stops both.
Everything except `make install` and `make check` runs inside containers.

## Local setup

```bash
git clone https://github.com/olah0la/django-forge.git
cd django-forge
make install        # create .venv and install runtime + dev dependencies
make check          # lint, type-check, run tests
```

That is the whole setup. `make help` lists every available task.

> **What you cannot do yet.** The stack serves a Django project backed by PostgreSQL — `make up`,
> `make migrate`, and open <http://localhost:8000>. What is still missing is **M5** onward: there
> are no models yet (M4-04), no API layer, and no production application server. Targets that drive
> files which do not exist yet tell you which issue delivers them rather than leaking a raw tool
> error. That is expected, not a broken setup.

## Hot-reload development loop

The development stack mounts your working copy into the container, so **an edit on your machine is
live inside the container immediately** — no rebuild, no restart.

```bash
make up          # start the development stack
# edit any file in your editor
# the change is already in the container
```

**Where code goes.** `config/` holds project configuration, `apps/` holds applications, and
`apps/core/` holds shared code. See [docs/layout.md](docs/layout.md) before adding an app — the
`AppConfig.name` must be the full dotted path (`apps.billing`, not `billing`).

**What is mounted.** The whole project directory, onto `/app`. That includes files you create after
the container started.

**When you still need a rebuild.** Dependency changes. `uv.lock` is installed into the image at build
time, not read from the mount, so after editing dependencies:

```bash
make lock        # re-resolve into uv.lock
make build       # reinstall them in the image
make up
```

**The development image carries test tooling; the production one does not.** `app` builds the
Dockerfile's `dev` stage (runtime plus ruff, mypy and pytest) so `make test-db` can run the suite
inside the network against the real database — its port is deliberately not published, so the host
cannot reach it. `app-prod` builds `runtime`, which has no test tooling at all. If you add a
development dependency, `make lock && make build` as above.

**The production-like stack is deliberately not mounted.** `make up-prod` runs the image exactly as
built, which is what makes it a faithful stand-in for a deployment. If you change a file and it does
not take effect there, that is correct behaviour, not a bug.

### Seed data

An empty database is a poor place to work, so one command produces a known state:

```bash
make migrate
make seed        # development only
```

That gives you:

| | |
| --- | --- |
| A superuser | `admin` / `admin`, from `DJANGO_SUPERUSER_*` if you set them |
| Group `read-only` | every `view_*` permission, and nothing else |
| Group `user-admin` | add/change/view on users and groups — deliberately no delete |

**Run it as often as you like.** It is idempotent: `get_or_create` on natural keys, and group
permissions are `set()` rather than added, so a second run converges instead of accumulating. It
also *re-asserts* the superuser's password every run — if you changed it by hand, seeding changes it
back, and says so in its output.

**It cannot run in production.** The command refuses unless `settings.SEED_ENABLED` is true, which
only the development and test layers set. That flag is deliberately not readable from the
environment: `.env` is shared by both Compose profiles, so a value there could arm the very thing
the guard prevents.

Add your project's own seed data at the marked extension point in
`apps/core/management/commands/seed.py`.

### Before something risky, dump

Seeding gets you a *known* database back. When what you want back is the one you had — the state you
spent an afternoon clicking into existence, or the data a migration is about to rewrite — dump it
first:

```bash
make db-dump                                  # → backups/forge-<timestamp>.dump
make migrate                                  # the risky thing
make db-restore FILE=backups/forge-<ts>.dump  # if it went wrong
```

Restoring **drops and rebuilds** the database, so it asks you to type the database name back before
it does (`FORCE=1` skips the prompt). Dumps are git-ignored and must stay that way — a dump holds
whatever was in your database, and this is a template that others clone.

This is a local convenience and **not a backup strategy**; the distinction, and the round trip
verified end to end, are in [docs/backups.md](docs/backups.md).

### Two things not to "fix"

**The virtualenv lives at `/opt/venv`, not `/app/.venv`.** It has to sit outside the mounted path:
the mount covers all of `/app`, and a venv underneath it would be hidden the moment the stack
starts, so every import would fail with a confusing "module not found". If you move it back, the
development stack breaks.

**The development image is built with your uid and gid.** The `Makefile` exports them from `id -u`
and `id -g`, and Compose passes them as build arguments. A bind mount preserves *numeric* ownership,
so without this the container writes files onto your machine owned by a user that does not exist,
and files you own can be unwritable inside the container. This bites on Linux; macOS hides it behind
its filesystem translation layer, so test on Linux before assuming it is fine.

## Picking something to work on

1. **Start with a [`good first issue`](https://github.com/olah0la/django-forge/issues?q=is%3Aissue+is%3Aopen+label%3A%22good+first+issue%22).**
   These are self-contained and chosen so you can finish one without holding the whole system in
   your head.
2. **Check the *Depends on* field.** If an issue it depends on is still open, pick something else.
   The roadmap is ordered deliberately, and starting downstream work early usually means redoing it.
3. **Read the acceptance criteria before you write anything.** They are written to be objectively
   checkable, and they are what "done" means.

> **An issue you cannot understand is a defect in the issue.** Every issue has a *Context* section
> explaining why the work exists. If it does not make the purpose clear to you, comment and say so —
> it will be rewritten. Issues that need tribal knowledge are why onboarding is slow.

## Branching

**Default: one branch per milestone.** Work on the milestone branch and make one commit per issue.

```
m1-foundations
m2/containerization
```

**Give an issue its own branch if any of these is true:**

- Its estimate is `L` or `XL`
- It changes migrations, security-relevant configuration, or dependency pins
- It modifies a file every downstream project inherits (`Dockerfile`, settings, `pyproject.toml`)
- It might need to be reverted independently of the rest of the milestone

```
m4/03-migration-review-checklist
```

If none of those apply, commit to the milestone branch. You do not need to make a judgement call
beyond that list.

## Commit messages

Start with the issue number, then an imperative summary of what the commit does.

```
#12 Add multi-stage Dockerfile with non-root runtime user
#13 Run the application as a dedicated non-root user
```

- **Imperative mood** — "Add", not "Added" or "Adds". It completes the sentence *"This commit
  will…"*.
- **One issue per commit** where practical. It keeps history readable and makes a revert surgical.
- **Explain *why* in the body** when the reason is not obvious from the diff. The diff already
  shows what changed; it cannot show what you ruled out.

```
#14 Pin base image to a specific digest

Tracking a floating tag meant two developers could build different images from
the same commit, which made a caching bug impossible to reproduce.
```

## Pull requests

Open a PR against the milestone branch (or `main` for a milestone branch itself). In the
description:

- **Reference the issue** it closes.
- **Say how you verified it.** Not "tested locally" — the actual command and what you observed.
- **Call out anything you were unsure about.** That is where review is most valuable.

### What reviewers check

1. **Every acceptance criterion is met**, and was *demonstrated* rather than assumed.
2. **The Definition of Done below is satisfied.**
3. **The change matches the issue's scope** — unrelated improvements belong in their own issue.
4. **A newcomer could understand it.** Comments explain *why*, not *what*.
5. **Nothing secret is committed.** No credentials, tokens, or real `.env` values.
6. **Nothing secret is *logged*.** A new log call must not pass a request body, a query string, or
   an unfiltered dictionary. If a mapping has to be logged, put it through `redact_mapping()`. See
   [docs/logging.md](docs/logging.md#what-is-never-logged-and-how).
7. **Endpoints are on an app's router, never on the API instance.** `config/api.py` mounts routers
   and defines none — see [docs/api.md](docs/api.md#routers-one-per-app).
8. **Input and output schemas are separate types, and response schemas are allow-lists.** A shared
   schema makes read-only fields writable and write-only fields visible. `fields = "__all__"` and
   `exclude = [...]` are both rejected: they expose the *next* field added to the model, in a file
   nobody touched, with no diff to review. See
   [docs/api.md](docs/api.md#response-schemas-are-allow-lists).
9. **List endpoints are paginated *and* ordered.** `RouterPaginated` handles the first, as long as
   the response is a collection type; the second is yours — offset pagination over an unordered
   queryset silently repeats and drops rows. See
   [docs/api.md](docs/api.md#order-by-is-load-bearing).

Reviewers ask questions to understand, not to challenge. If a comment reads as blunt, assume it is
brevity rather than criticism — and if a review comment is unclear, ask.

## Migration review checklist

Migrations get their own checklist because they are the most common cause of serious production
incidents in Django projects, and because the danger is invisible locally: an operation that returns
instantly against fifty development rows can lock a fifty-million-row production table for minutes.

Work through this on any PR that adds or changes a file under `*/migrations/`. The reasoning behind
every line, with worked examples, is in [docs/migrations.md](docs/migrations.md).

**Before requesting review**

- [ ] `make migrations-check` passes — no model change is missing a migration
- [ ] The migration was **read**, not just generated. A migration is code and is reviewed as code
- [ ] Django chose the operation you meant — in particular, a `RenameField` is a rename and not a
      drop-plus-add of the same data
- [ ] `make migrate` was run, then run **again**, and the second run reported `No migrations to apply.`

**The four that cause outages** — if the PR does any of these, say so in the description and say why
it is safe:

- [ ] **Adding a non-nullable column without a constant default.** A computed default (`now()`, a
      UUID) rewrites every row under an exclusive lock. Add the column nullable, backfill, then add
      the constraint `NOT VALID` and `VALIDATE` it — across separate deploys
- [ ] **Adding an index without `CONCURRENTLY`.** A plain `AddIndex` blocks writes for the whole
      build. Use `AddIndexConcurrently` with `atomic = False` on the migration — without that flag it
      does not run at all
- [ ] **Renaming or dropping a column in a single deploy.** During a rolling deploy old and new code
      run against one schema, so one half breaks. Use add → backfill → switch reads → drop, over four
      deploys
- [ ] **A data migration that loads the whole table.** `Model.objects.all()` materialises every row.
      Batch with `.iterator()` or pk ranges, use `apps.get_model()` rather than importing the model,
      and give `RunPython` a reverse

**Other things a reviewer catches**

- [ ] No applied migration was edited in place — changes are additive, in a new migration
- [ ] A branched history was resolved with a merge migration whose branch summary was checked for
      *semantic* conflicts, not only ordering
- [ ] A non-atomic migration (`atomic = False`) does one thing, since it cannot roll back
- [ ] Any squash keeps the originals in place for one release before they are deleted

## Definition of Done

Every task inherits this checklist. Confirm each item before asking for review.

- [ ] All acceptance criteria are met and were **demonstrated, not assumed**
- [ ] The change is documented where a future reader will look for it
- [ ] `.env.example` and `README.md` are updated if behaviour or setup changed
- [ ] The full stack starts cleanly from a fresh clone after the change
- [ ] The change has been reviewed and approved by at least one other engineer

## Asking questions

A question asked early costs minutes. A wrong assumption discovered in review costs a day. If you
are unsure what an issue means, which approach is wanted, or why something is the way it is, ask
before building — that is a normal part of the work, not a sign you are behind.

---

## This guide grows

Sections are added as the milestones that need them land. Do not write these early; there is no
implementation to describe yet.

| Section to come | Added by |
| --- | --- |
| API schema and review expectations | **M5-03** |
| How to add an ADR | **M7-03** |
