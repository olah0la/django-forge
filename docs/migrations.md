# Migrations

A **migration** is a versioned, ordered description of a schema change. Django generates them from
your model edits and runs them in sequence to bring any database to the current schema.

They are also the most common cause of serious production incidents in Django projects. The reason
is not complexity — it is that migrations are *tested against a database with fifty rows and run
against one with fifty million*. `ALTER TABLE` operations that return instantly on the former can
hold an `ACCESS EXCLUSIVE` lock on the latter for minutes, and every query against that table waits
behind the lock. From outside, that is indistinguishable from an outage.

This document covers the everyday loop, the operations that cause those incidents, and the two
situations that reliably confuse people the first time: **branched history** and **squashing**.

The short version a reviewer uses in a pull request is the
[migration review checklist](../CONTRIBUTING.md#migration-review-checklist) in `CONTRIBUTING.md`.

---

## The everyday loop

```bash
make makemigrations      # generate from model changes
                         # -- then READ the generated file --
make migrations-check    # fail if any model change has no migration
make migrate             # apply
make db-shell            # inspect the result in psql
```

All four run **inside the container**, where `DATABASE_URL` is supplied. That is not incidental:
settings deliberately have no SQLite fallback, so running `manage.py` on the host fails with a
message telling you to use these targets rather than quietly migrating a different engine. See
[layout.md](layout.md#connecting-to-it).

### Read the generated file

The step between generating and applying is the one that gets skipped, and it is the only one that
prevents an incident. **A migration is code.** It is committed, reviewed, and shipped like any other
file — the fact that a tool wrote it makes it *less* trustworthy in review, not more, because nobody
formed an intention about its contents.

What you are reading for is the [dangerous operations](#the-operations-that-cause-outages) below,
and one structural question: **is the operation Django chose the one you meant?** Renaming a field
is the classic case — Django cannot tell a rename from "drop one column, add another", and the
difference is your entire dataset.

### `make migrations-check`

```console
$ make migrations-check
No changes detected
```

Exit status is what matters: non-zero means a model was edited and the migration was never
generated. That is the "works locally, fails on deploy" defect, and it is invisible locally because
your database already has the column — you added it by hand, or your branch had the migration and
you rebased it away.

With drift present it names what is missing and fails:

```console
$ make migrations-check
Migrations for 'core':
  apps/core/migrations/0001_initial.py
    + Create model Scratch
make: *** [Makefile:210: migrations-check] Error 1
```

`--dry-run` guarantees it writes nothing, so it is safe to run anywhere. Run it before pushing.

> It is deliberately **not** part of `make check`. That target runs on the host; this one needs the
> container. Folding it in would make the pre-push gate fail whenever the stack happens to be down,
> which trains people to ignore it.

---

## Applying twice is safe

Migrations are idempotent at the level of the *migration*, not the SQL inside it. Django records
every applied migration in the `django_migrations` table and consults it before running anything, so
a second `migrate` is a no-op:

```console
$ make migrate
Running migrations:
  Applying core.0001_initial... OK

$ make migrate
Running migrations:
  No migrations to apply.
```

Two consequences worth internalising:

- **Re-running a deploy is not dangerous.** If a deploy fails after migrating, the retry skips the
  migrations it already applied.
- **The bookkeeping is in the database, not in the files.** Deleting a migration file that has been
  applied does not unapply it; it leaves a row pointing at a file that no longer exists. To undo a
  migration, `migrate <app> <previous_migration>` — then delete the file.

## Why migrations do not run at startup

The most common thing to put in a Django entrypoint is the thing this template deliberately refuses
to put there. `docker-entrypoint.sh` waits for the database and hands off; it never migrates.

- During a rolling deploy **every replica starts at once**, and they race to apply the same
  migration.
- A long migration **blocks startup past the platform's health timeout**, so the container is killed
  mid-migration and restarted — repeatedly.

Migrations are therefore run as a deliberate step: `make migrate` locally, and a release job or a
one-off task in a deployment. If they ever must run in the entrypoint, they have to be gated behind
an opt-in variable *and* restricted so only one replica can run them at a time.

---

## The operations that cause outages

Four operations account for most migration incidents. Each is fine on an empty table and each is an
outage on a large one.

### 1. Adding a non-nullable column

**What breaks.** `NOT NULL` with no default cannot be applied to a table with existing rows at all —
PostgreSQL rejects it. The subtler case is the one that *succeeds*: a **volatile** default (`now()`,
a UUID, anything computed per row) must write every row, which rewrites the whole table under an
exclusive lock.

**The nuance, stated honestly.** Since PostgreSQL 11, adding a column with a **constant** default is
metadata-only and effectively instant — the old advice that "any default rewrites the table" is out
of date. Django emits a constant default as such. So `models.CharField(default="unknown")` on a huge
table is fine, and `models.UUIDField(default=uuid4)` is not, because Django computes that per row.

**The safe form** when the default cannot be constant:

1. Add the column **nullable**, no default. Instant.
2. Backfill in batches (see [4](#4-a-data-migration-that-loads-the-whole-table)).
3. Add the constraint separately, `NOT VALID` first so it only checks new rows, then `VALIDATE
   CONSTRAINT`, which takes a weaker lock that does not block reads and writes.

Steps 1 and 3 belong to **different deploys**, because between them the application must tolerate
`NULL`.

### 2. Adding an index without `CONCURRENTLY`

**What breaks.** A plain `CREATE INDEX` takes a lock that blocks **writes** to the table for as long
as the build takes. On a large table that is minutes, and every insert and update queues behind it.

**The safe form** is `AddIndexConcurrently`, which builds the index without blocking writes:

```python
from django.contrib.postgres.operations import AddIndexConcurrently
from django.db import migrations, models


class Migration(migrations.Migration):
    atomic = False          # mandatory — see below

    dependencies = [("core", "0003_previous")]

    operations = [
        AddIndexConcurrently(
            model_name="scratch",
            index=models.Index(fields=["name"], name="scratch_name_idx"),
        ),
    ]
```

**`atomic = False` is mandatory, not stylistic.** Django wraps each migration in a transaction, and
PostgreSQL cannot build an index concurrently inside one. Omitting it does not degrade to a slow
index — the migration fails outright:

```
django.db.utils.NotSupportedError: The AddIndexConcurrently operation cannot be
executed inside a transaction (set atomic = False on the migration).
```

The cost you accept in exchange: a non-atomic migration that fails partway **does not roll back**.
A concurrent build that is interrupted leaves an `INVALID` index behind, which must be dropped
before retrying. Keep non-atomic migrations to the single operation that needs them.

### 3. Renaming or dropping a column in one deploy

**What breaks.** This is the one that is genuinely unintuitive, and the reason is not the database —
it is the deploy. During a rolling deploy, **old and new application code run simultaneously against
one schema**. There is no instant at which the code changes over.

So a single-deploy rename breaks whichever half is out of step. Rename `name` to `full_name` in one
migration and the old replicas — still serving traffic — query a column that no longer exists.
Every one of their requests errors until they are replaced. The same is true of a drop: the old code
still selects the column.

**The safe form is four deploys.** It looks like a lot of ceremony for a rename, and it is the
difference between a rename and an incident:

| Deploy | Schema | Code |
| --- | --- | --- |
| **1. Add** | Add `full_name`, nullable | Write to **both** `name` and `full_name`; read `name` |
| **2. Backfill** | — (data migration) | Unchanged |
| **3. Switch** | — | Read `full_name`; still write both |
| **4. Drop** | Drop `name` | Write only `full_name` |

Each deploy is safe with either version of the code running. Deploy 1 must ship before its backfill,
deploy 3 must not ship until the backfill is verified complete, and deploy 4 must not ship until no
running code reads `name`.

For a **drop** alone, the short version is: stop referencing the column, deploy, *then* drop it in a
later deploy. Never in the same one.

> Django's `RenameField` is not wrong — it is right for a table nobody is querying yet, and for a
> pre-launch project. Its danger is that it looks equally harmless afterwards.

### 4. A data migration that loads the whole table

**What breaks.** `Model.objects.all()` inside `RunPython` materialises **every row into memory**. At
fifty million rows the migration process is killed by the OOM killer partway through — and if the
migration is atomic, everything rolls back after all that work; if it is not, it stops half-done.

**The safe form:**

```python
def backfill(apps, schema_editor):
    # apps.get_model, never a direct import: this migration must keep working
    # against the model as it was HERE, not as it is today. A direct import
    # breaks the moment someone adds a field in a later migration.
    Scratch = apps.get_model("core", "Scratch")

    qs = Scratch.objects.filter(full_name__isnull=True).only("id", "name")
    for obj in qs.iterator(chunk_size=2000):
        obj.full_name = obj.name
        obj.save(update_fields=["full_name"])


class Migration(migrations.Migration):
    operations = [
        migrations.RunPython(backfill, migrations.RunPython.noop),
    ]
```

Three details that are each their own bug when missed:

- **`apps.get_model()`, never a direct import.** A migration is a historical record. Importing the
  live model makes it depend on today's fields, so it breaks retroactively when the model changes.
- **`.iterator(chunk_size=...)`**, or explicit primary-key ranges. Both bound memory; pk-ranges are
  preferable when the backfill must be resumable, because each batch commits independently.
- **A reverse.** `RunPython` with no `reverse_code` makes the whole migration irreversible, which
  blocks rolling back everything after it too. `RunPython.noop` is a legitimate reverse when undoing
  the data genuinely is a no-op — say so in a comment rather than leaving it blank.

Large backfills are usually better run as a **management command outside the migration**, so they
can be monitored, throttled, and resumed. Use a data migration when the change is small and must be
atomic with the schema change; use a command when it is long.

### Quick reference

| Operation | Verdict |
| --- | --- |
| Add a nullable column | ✅ Safe |
| Add a column with a **constant** default | ✅ Safe (PostgreSQL 11+, metadata-only) |
| Drop an index | ✅ Safe |
| Add a column with a **computed** default | ⚠️ Rewrites the table — add nullable, then backfill |
| Add an index | ⚠️ `AddIndexConcurrently` + `atomic = False` |
| Add `NOT NULL` / a `CHECK` | ⚠️ `NOT VALID`, then `VALIDATE CONSTRAINT` |
| A data migration | ⚠️ Batch it; consider a management command instead |
| Change a column type | ⚠️ Usually a rewrite — treat as add/backfill/switch/drop |
| **Rename** a column | 🚫 Never in one deploy |
| **Drop** a column still referenced by running code | 🚫 Never in one deploy |

---

## Conflicting migrations from parallel branches

This happens on any team working in parallel, and it looks alarming the first time.

Two developers branch from the same migration. Each adds a model change and generates `0002_*`. Both
merge. The migration history now has **two leaves** — two migrations claiming the same parent — and
Django cannot determine an order, so it refuses to run anything at all:

```console
$ make migrate
CommandError: Conflicting migrations detected; multiple leaf nodes in the migration graph:
(0002_scratch_colour, 0002_scratch_size in core).
To fix them run 'python manage.py makemigrations --merge'
```

`make makemigrations` fails with the same message. **Nothing is broken and no data is at risk** —
Django has stopped before doing anything.

### The fix: a merge migration

```bash
docker compose exec app python manage.py makemigrations --merge
```

```console
Merging core
  Branch 0002_scratch_colour
    + Add field colour to scratch
  Branch 0002_scratch_size
    + Add field size to scratch

Created new merge migration .../0003_merge_0002_scratch_colour_0002_scratch_size.py
```

The merge migration contains **no operations**. It exists purely to depend on both leaves and
give the graph a single tip again:

```python
class Migration(migrations.Migration):
    dependencies = [
        ("core", "0002_scratch_colour"),
        ("core", "0002_scratch_size"),
    ]

    operations = []
```

`make migrate` then applies both branches and the merge, and the history is linear again.

**Read the branch summary before accepting it.** `--merge` resolves the *ordering*, and it cannot
know whether the two changes are compatible. Two branches that each added a differently-named
column are fine. Two branches that both added an `email` field, or that changed the same field in
incompatible ways, produce a graph Django is happy with and a schema you did not intend. That is
what the printed summary is for.

### When to regenerate instead

Merging is the default because it is safe with migrations that have already been applied anywhere.
Delete-and-regenerate — dropping your branch's migration, rebasing, and running `makemigrations`
again — gives a cleaner linear history, but it is only safe when **your migration has never been
applied outside your own machine**. If a colleague, a review environment, or CI has applied it,
their `django_migrations` table records a migration that no longer exists.

The rule underneath both: **an applied migration is never edited in place.** Editing one changes
what future installs run while leaving every existing database on the old version, and the two
silently diverge. Add a new migration instead.

---

## Squashing

`squashmigrations` collapses a run of migrations into one, optimising away operations that cancel
out — a field added and later removed disappears entirely.

```bash
docker compose exec app python manage.py squashmigrations core 0003_merge_...
```

```console
Will squash the following migrations:
 - 0001_initial
 - 0002_scratch_size
 - 0002_scratch_colour
 - 0003_merge_0002_scratch_colour_0002_scratch_size
Optimizing...
  Optimized from 3 operations to 1 operations.
```

**When it is appropriate:**

- The history is long enough that building the test database is measurably slow. That is the actual
  payoff, and it is the only one worth a risky operation.
- At a **release boundary**, not mid-feature.
- When every migration being squashed has been applied **everywhere** — all environments and every
  developer's local database.

**When it is not:** to tidy up. A long migration history is not a problem, it is a record. Squashing
a history that is not causing pain trades a real risk for an aesthetic gain.

### The two-stage replacement

The output says this, and it is the part people skip:

> You should commit this migration but leave the old ones in place; the new migration will be used
> for new installs. Once you are sure all instances of the codebase have applied the migrations you
> squashed, you can delete them.

The squashed migration declares `replaces = [...]`, which lets Django recognise a database that
applied the originals as already up to date. That only works while the originals are still present.

1. **Deploy 1** — commit the squashed migration, keep the originals. New installs take the short
   path; existing databases are recognised as current.
2. **Later** — once every environment is past that point, delete the originals and remove the
   `replaces` attribute.

Deleting the originals in one step strands any database that had applied them but not the squash.

### The `RunPython` caveat

The optimiser cannot see inside `RunPython` or `RunSQL`. It cannot reorder around them or collapse
them, so a history full of data migrations squashes poorly — sometimes to no benefit at all. Worse,
a data migration written against a model that the squash has since changed can break when replayed
on a fresh install.

Before squashing a history containing data migrations, ask whether those migrations still need to
exist. A backfill for a column that every database has long since backfilled is often better
replaced with a `RunPython.noop`, or deleted along with the range being squashed.

---

## See also

- [migration review checklist](../CONTRIBUTING.md#migration-review-checklist) — the PR-time short form
- [layout.md](layout.md#the-database) — the database service, `DATABASE_URL`, connection reuse
- [Django: migration operations](https://docs.djangoproject.com/en/5.2/ref/migration-operations/)
- [PostgreSQL: `ALTER TABLE`](https://www.postgresql.org/docs/17/sql-altertable.html) — which forms
  take which lock
