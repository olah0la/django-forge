# Models

Every model in a project built from this template should start by inheriting from
`apps.core.models.BaseModel`. This page covers what that gives you, what it costs, and the two
patterns that are deliberately **not** included.

---

## The base models

```python
from apps.core.models import BaseModel, TimeStampedModel, UUIDModel
```

| Class | Provides |
| --- | --- |
| `UUIDModel` | `id` — a UUIDv7 primary key |
| `TimeStampedModel` | `created_at`, `updated_at` |
| `BaseModel` | Both. The default choice. |

They are separable on purpose: a table can take the timestamps while keeping Django's integer
primary key, or the reverse. `BaseModel` exists because taking both is the common case.

All three are **abstract** — they get no table of their own, and their fields are copied into each
concrete model that inherits them. Verified directly against `information_schema` in the test
database: no table exists for any of the three, while their concrete subclasses each have one.

### Worked example

```python
from django.db import models

from apps.core.models import BaseModel


class Invoice(BaseModel):
    reference = models.CharField(max_length=32, unique=True)
    total_cents = models.PositiveIntegerField()
    issued_to = models.ForeignKey("customers.Customer", on_delete=models.PROTECT)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(total_cents__gte=0),
                name="invoice_total_not_negative",
            ),
        ]
```

What `Invoice` did not have to declare, and now has:

```python
invoice = Invoice.objects.create(reference="INV-001", total_cents=1000, issued_to=customer)

invoice.pk          # UUID('01a047a9-...'), version 7, generated in Python before INSERT
invoice.created_at  # set once, never changed again
invoice.updated_at  # refreshed on every save()
```

Because the primary key is generated in Python rather than by a database sequence, **`invoice.pk` is
known before the row is written**. That is occasionally useful: you can build related objects
referencing it inside a single transaction without an intermediate flush.

---

## Why UUIDv7 and not a sequential integer

A `BigAutoField` in a URL leaks two things. `/invoices/1042/` says there are roughly 1042 invoices,
and it says the neighbours are at `1041` and `1043`. A template cannot know whether its adopters
will expose IDs publicly, so the default has to be the one that survives that being true.

### Why v7 rather than v4

Both are equally opaque — that is not the difference. The difference is **where the inserts land in
the index**. v4 is uniformly random, so consecutive inserts touch unrelated index pages scattered
across the whole B-tree. v7 leads with a 48-bit millisecond timestamp, so new rows cluster in a
small, hot region at the right-hand edge.

Measured on this stack, inserting 50,000 rows into a table already holding 300,000:

| | UUIDv4 | UUIDv7 |
| --- | --- | --- |
| Index pages read from disk | 1,157 | **3** |
| Index pages dirtied | 2,565 | **540** |
| Wall clock | 95 ms | **56 ms** |

Dirtied pages are the number that matters operationally: they become WAL volume and checkpoint I/O
on every write. The gap widens as the index outgrows RAM, which is the case this default is chosen
for.

### What it is *not*

**v7 does not produce a smaller index.** This is widely repeated and it is wrong. Measured at 300,000
rows, the primary key index came to exactly **9,486,336 bytes for both** v4 and v7 after `REINDEX`.
Before reindexing, the v7 index was in fact *11% larger* than the v4 one — page-split history from
the insert pattern, not a steady-state property.

Both store 16 bytes per value. If you read that v7 saves space, it was not measured.

### What it costs

- **16 bytes against 8**, in this index and in every foreign key that references it.
- **Unreadable in logs** and support conversations. "Invoice 1042" is a sentence; a UUID is not.
- **Sortable only to the millisecond.** See below.

### The millisecond limit, stated precisely

`uuid7()` is time-sortable, but only at millisecond granularity. Within a single millisecond the low
bits are random and the order is undefined.

Measured over 200,000 identifiers generated in a tight loop (about 775 per millisecond):

- Ordering **across** millisecond boundaries: correct in 257 of 257 cases.
- Ordering **within** a millisecond: about **half** of adjacent pairs are out of order — 99,915 of
  199,999.
- Collisions: **zero**.

So `ORDER BY id` is a reasonable proxy for creation order at human timescales and **not** a
substitute for a real ordering. If exact insertion order matters, order by `created_at` and break
ties explicitly.

### When to override it

Declare your own `id` and skip `UUIDModel` when the table is internal-only with no ID ever exposed,
or large enough that 8 bytes per row and per reference genuinely matters. That is a deliberate
choice, which is exactly why the primary key lives in a class you can decline:

```python
class LedgerEntry(TimeStampedModel):    # timestamps, but a compact integer key
    ...
```

Django's own tables — `auth_user`, `django_admin_log`, `django_session` — keep integer primary keys.
`DEFAULT_AUTO_FIELD` is still `BigAutoField`, and that is correct: those tables are internal and
never appear in a URL you control.

> **Whichever you choose, choose it before there is data.** Changing a primary key type afterwards
> means rewriting the table and every foreign key that points at it. See
> [migrations.md](migrations.md).

---

## Timestamps, and the write that skips them

`created_at` and `updated_at` are applied **in Python**, not by a database default. Which writes get
them is not intuitive, so it was measured rather than assumed:

| Write | `created_at` | `updated_at` |
| --- | --- | --- |
| `Model.objects.create(...)` | set | set |
| `instance.save()` | untouched | refreshed |
| `Model.objects.bulk_create([...])` | **set** | **set** |
| `Model.objects.filter(...).update(...)` | untouched | **NOT refreshed** |
| `Model.objects.bulk_update([...], [...])` | untouched | **NOT refreshed** |

`bulk_create()` works because Django calls each field's `pre_save()` while compiling the `INSERT` —
it is commonly lumped in with the bypassing methods and it does not belong there.

`update()` and `bulk_update()` compile straight to SQL and never touch the field. The row is
written, **no error is raised**, and `updated_at` silently goes stale. When using either, set the
column explicitly:

```python
from django.utils import timezone

Invoice.objects.filter(status="draft").update(status="sent", updated_at=timezone.now())
```

`created_at` carries `db_index=True`: "most recent first" is the overwhelmingly common ordering for
these models, and adding the index later means an `AddIndexConcurrently` migration against a
populated table.

---

## Soft deletion — documented, not shipped

**There is no soft-delete mixin here, deliberately.** The pattern is reasonable and it is not free,
and a template's defaults should surprise nobody. A project that wants it should add it knowingly.

The pattern is a `is_deleted` flag (or `deleted_at` timestamp) plus a default manager that filters
the marked rows out:

```python
class NotDeletedManager(models.Manager):
    def get_queryset(self):
        return super().get_queryset().filter(deleted_at__isnull=True)


class SoftDeleteModel(BaseModel):
    deleted_at = models.DateTimeField(null=True, blank=True, db_index=True)

    objects = NotDeletedManager()      # the filtered default
    all_objects = models.Manager()     # the unfiltered escape hatch

    class Meta:
        abstract = True

    def delete(self, *args, **kwargs):
        self.deleted_at = timezone.now()
        self.save(update_fields=["deleted_at"])
```

Three costs, stated plainly, because each one is discovered the hard way:

1. **A row that is visibly in the database appears not to exist.** Someone debugging a "missing"
   record can see it in `psql` while the ORM insists it is gone. This is the single most common
   confusion the pattern causes, and it costs hours the first time.
2. **Unique constraints collide with deleted rows.** Delete a user with email `a@example.com` and
   you cannot create another with that address — the old row still holds it. The fix is a partial
   unique index (`condition=Q(deleted_at__isnull=True)`), which is easy to forget and invisible
   until a real user hits it.
3. **Every raw query and every join has to remember the filter.** The default manager covers the
   ORM's front door and nothing else: `RawSQL`, a `JOIN` written by hand, a reporting query, an
   aggregate over a related set. Each is a place the filter can be missed silently.

Also note that `QuerySet.delete()` bypasses the model's `delete()` method entirely — a bulk delete
will *really* delete soft-deleted-model rows unless the manager overrides it too.

If what you actually need is an audit trail rather than recoverable deletes, a separate history
table is usually the better answer: it keeps the live table clean and the questions it answers
("who changed this, and when") are the ones people are really asking.

---

## Conventions for indexes, constraints, and naming

- **Name every constraint and index explicitly.** Django will generate a name, and the generated
  ones are unreadable hashes that make migrations and `psql` output hard to follow. `name=` costs
  nothing at write time and pays back at debug time.
- **Prefer `Meta.constraints` over `unique_together`.** `constraints` covers uniqueness, check
  constraints, conditional (partial) uniqueness, and exclusion constraints in one place;
  `unique_together` does only the first and is effectively legacy.
- **Enforce invariants in the database, not only in `clean()`.** Model validation runs only when
  something calls it — `bulk_create`, `update()`, a management command and any other client of the
  database all skip it. A `CheckConstraint` cannot be bypassed.
- **`db_index=True` on a single field, `Meta.indexes` for composites.** A composite index serves
  queries that filter on a *prefix* of its columns, so column order is a decision, not a formality.
- **An index is not free.** Every one is maintained on every write and adds to the table's storage.
  Add them for queries you have, not for queries you imagine.
- **Adding an index to a populated table needs `AddIndexConcurrently`** — see
  [migrations.md](migrations.md#2-adding-an-index-without-concurrently).

---

## A field added here is not API surface

Adding a column does not publish it. Responses are built from schemas that name their fields
explicitly, so a new field reaches clients only when somebody adds it to a schema — which is the
review moment the convention exists to create. That guarantee holds only as long as no schema uses
`fields = "__all__"` or `exclude`, both of which turn the next field you add into public API with no
diff to catch. See [api.md](api.md#response-schemas-are-allow-lists); a test enforces it.

The same applies to `help_text`: through a `ModelSchema` it becomes the field's description in the
published OpenAPI document, so it is read by API consumers as well as by admin users.

---

## See also

- [api.md](api.md#schemas) — how a model becomes a request and response contract
- [migrations.md](migrations.md) — generating, reviewing, and applying schema changes safely
- [layout.md](layout.md) — where models live and how apps are registered
- `apps/core/models.py` and `apps/core/uuid7.py` — the implementations, commented with the reasoning
