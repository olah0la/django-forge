# Local backup and restore

> ## ⚠️ This is not a backup strategy
>
> `make db-dump` writes a file to your laptop. That is a **development convenience** — a way to
> capture local state before a risky migration and get it back afterwards.
>
> It is not a backup policy, and nothing here should be copied into one. A dump on the same machine
> as the database it came from protects against exactly one failure: your own next command. It does
> not protect against a lost laptop, a deleted volume you forgot to dump first, or a corruption you
> only notice next week. [What a real strategy needs](#why-this-is-not-a-production-backup-strategy)
> is at the bottom of this page, and it is a longer list than most people expect.
>
> A documented dump command is easily mistaken for a backup policy. The difference becomes apparent
> at the worst possible moment.

With that established: locally, this is genuinely useful.

---

## The everyday loop

```bash
make db-dump                                  # before something risky
make migrate                                  # the risky thing
make db-restore FILE=backups/forge-<ts>.dump  # when it went wrong
```

The first is the habit worth building. [migrations.md](migrations.md#the-operations-that-cause-outages)
lists the operations that turn a schema change into an outage; a dump taken beforehand is what turns
"I have to rebuild my database" into a thirty-second detour.

```console
$ make db-dump

  Dumped forge -> backups/forge-20260828-164751.dump (32K)
```

Dumps land in `backups/`, which is git-ignored — anchored as `/backups/`, so an *application*
directory named `backups/` stays trackable. Override the destination with `FILE=`, and the directory
itself with `BACKUP_DIR=`.

**They are never committed, and this matters more here than in an ordinary project.** A dump holds
whatever was in your database. django-forge is a template: a file committed here is inherited by
every project forged from it.

---

## What is actually in the dump

One database, in PostgreSQL's custom format: schema and data, compressed, with a table of contents.

```console
$ file backups/forge-20260828-164751.dump
backups/forge-20260828-164751.dump: PostgreSQL custom database dump - v1.16-0
```

Just as important, what is **not** in it:

| Not included | Consequence |
| --- | --- |
| Roles and passwords | Cluster-level, not database-level. `pg_dumpall --roles-only` is the tool. |
| Other databases in the cluster | `pg_dump` takes one database, named on the command line. |
| The volume itself | `docker compose down -v` destroys the cluster. A dump restores your *data*, not your database service. |
| Anything written after the dump ran | It is a point-in-time copy, and the point in time is in the filename. |

### Reading one

The custom format is compressed, so `less` is no help. `pg_restore -l` prints the table of contents:

```console
$ docker compose exec -T db pg_restore -l < backups/forge-20260828-164751.dump | head -5
;
; Archive created at 2026-08-28 14:48:25 UTC
;     dbname: forge
;     TOC Entries: 81
;     Compression: gzip
```

When you genuinely want readable SQL — to diff two schemas, or to paste one table's inserts
somewhere — take a plain-text dump instead. It is the same tool with a different format flag:

```bash
docker compose exec -T db pg_dump -U forge -d forge | gzip > backups/forge.sql.gz
```

The custom format is the default here because it is what real backups use, and because it can be
restored selectively — a single table out of a whole database, which plain SQL cannot do
(tradeoff 68).

### Why both commands run inside the container

`pg_dump` and `pg_restore` execute in the `db` container, so client and server are always the same
17.6 build. A host-installed client of an older major version refuses outright — run against this
stack's database from a machine with the PostgreSQL 16 client:

```console
$ pg_dump -h 172.29.0.2 -U forge -d forge --format=custom > out.dump
pg_dump: error: aborting because of server version mismatch
pg_dump: detail: server version: 17.6 (Debian 17.6-2.pgdg13+1); pg_dump version: 16.15 (Ubuntu 16.15-1.pgdg22.04+2)
```

That skew is the classic way a backup command that worked for a year starts failing after an
unrelated upgrade — of the *client*. It is also confusingly inconsistent between machines: on
Debian and Ubuntu, `/usr/bin/pg_dump` is a wrapper that picks a client matching the server if one
happens to be installed, so the same command succeeds on one laptop and fails on the next. (Both
behaviours above were reproduced on this machine, which has clients 16, 17 and 18 installed — the
wrapper succeeded, `/usr/lib/postgresql/16/bin/pg_dump` produced the error.)

Running both sides from one pinned image removes the failure mode rather than documenting it. It
also means no local PostgreSQL installation is needed, the same reasoning as
[`make db-shell`](layout.md#connecting-to-it).

---

## Restoring

```console
$ make db-restore FILE=backups/forge-20260828-164751.dump

  This DROPS the database "forge" and replaces it with
  the contents of backups/forge-20260828-164751.dump.
  Anything not in that file is gone, with no undo.

  Type the database name to continue: forge

  Restarting app: its pooled connections were terminated by the drop.

  Restored forge from backups/forge-20260828-164751.dump — 10 tables in public.
```

`FORCE=1` skips the prompt. Type it out when you mean it.

**The database is dropped and recreated, not cleaned in place.** `pg_restore --clean` drops objects
one at a time in archive order and fails on anything the dump does not know about, which leaves a
half-restored database — the worst possible outcome for a recovery tool. An empty database has no
ordering to get wrong (tradeoff 69).

Three consequences worth knowing before you need them:

**Every connection to that database is terminated.** `DROP DATABASE` refuses while anyone is
connected, and `DJANGO_CONN_MAX_AGE` keeps the app's connections open across requests, so the drop
uses `WITH (FORCE)` to close them. That is why the target restarts the `app` service afterwards —
without it the application holds handles to a database that no longer exists, and serves errors
until the connections happen to recycle.

**The dump is checked before anything is dropped.** The target lists the archive with
`pg_restore -l` first. This is not decoration: the first version of the target dropped the database
and only then discovered the file was unreadable, which is a recovery tool that destroys your data
when handed the wrong path. Both a non-archive and a truncated archive are now refused with the
database untouched.

**A restore is not a migration.** The restored database is at whatever schema version the dump held.
Run `make migrations-check` afterwards; if the dump predates a migration on your branch,
`make migrate` brings it forward.

---

## The round trip, verified

The point of a backup you have never restored is mostly psychological. This one was measured
end to end on PostgreSQL 17.6:

```bash
make migrate && make seed          # known state
make db-dump                       # 29,994 bytes, 0.42s

docker compose exec -T db psql -U forge -d postgres \
  -c 'DROP DATABASE forge WITH (FORCE)' -c 'CREATE DATABASE forge'
                                   # tables=0 — genuinely gone

make db-restore FILE=backups/forge-20260828-164751.dump   # 1.3s
```

| | Before | After |
| --- | --- | --- |
| Tables in `public` | 10 | 10 |
| Users | 1 (`admin`) | 1 (`admin`) |
| Groups | `read-only`, `user-admin` | `read-only`, `user-admin` |
| Group permissions | 13 | 13 |
| Applied migrations | 18 | 18 |

`make migrations-check` reported no drift afterwards and the application answered `200`.

**Do this yourself once**, on a database you do not mind losing, before you need it on one you do.
An untested backup is a hypothesis.

---

## Why this is not a production backup strategy

Everything above is one command writing one file to one disk. A real strategy is a different kind of
thing, and the gap is not effort — it is that each item below addresses a failure this cannot:

- **Continuous archiving and point-in-time recovery.** A dump restores you to when it ran. WAL
  archiving restores you to any moment, including one minute before the bad `DELETE` — which is the
  recovery people actually need, because they discover the mistake after it.
- **Off-host and off-account storage.** A copy on the same machine dies with the machine. A copy in
  the same cloud account dies with a compromised credential, and ransomware looks for backups first.
- **Encryption at rest, and access control on the backups.** A database dump is a complete copy of
  your data with none of the database's access control in front of it.
- **Retention and rotation.** Corruption is often noticed weeks later. If you keep one copy, you
  will faithfully have overwritten every good one with the bad one.
- **Monitoring that the backup ran.** Backups fail silently — a full disk, an expired credential, a
  renamed database. The alert you need is "no successful backup in 24 hours", and nothing produces
  it here.
- **Rehearsed restores, on a schedule.** Untested backups fail at restore time, when there is no
  second chance. The measurement that matters is how long a restore takes and whether anyone has
  done one this quarter.

**In practice, for most teams, the answer is the managed provider's own facility** — RDS automated
backups and snapshots, Cloud SQL backups, whatever the platform offers — because it does the list
above by default. Then add the part providers do not do for you: **restore into a scratch instance
on a schedule and confirm the data is there.**

The template deliberately makes no choice here (see the "Deployment target" row of the decisions
register). Backup design belongs to the deployment, which the template does not own.

---

## See also

- [migrations.md](migrations.md) — the workflow this is the safety net for, and the operations that
  make a dump worth taking first
- [layout.md](layout.md#the-database) — the database service, and
  [which commands destroy your data](layout.md#️-which-command-destroys-your-data)
- [CONTRIBUTING.md](../CONTRIBUTING.md#seed-data) — `make seed`, the other way to get a usable
  database back
- [PostgreSQL: backup and restore](https://www.postgresql.org/docs/17/backup.html) — including
  continuous archiving and PITR
- [`pg_dump`](https://www.postgresql.org/docs/17/app-pgdump.html) ·
  [`pg_restore`](https://www.postgresql.org/docs/17/app-pgrestore.html)
