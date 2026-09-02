# Serving the application

How a request reaches Django, in both profiles, and why each tuning value is what it is.

**Read this before changing a `GUNICORN_*` variable.** Two of the settings here are routinely
misread, and both misreadings cause production incidents: `timeout` does not bound a request, and
worker count multiplies against PostgreSQL's connection limit.

Everything is configured in one place — [`config/gunicorn.py`](../config/gunicorn.py) — and every
value in it reads an environment variable. The variables are listed with their defaults in
[`.env.example`](../.env.example).

```bash
make up-prod                                  # gunicorn + uvicorn workers, port 8001
curl localhost:8001/api/v1/ping
make logs                                     # "Using worker: uvicorn_worker.UvicornWorker"
```

---

## What runs where

Both profiles serve the same ASGI application, `config.asgi:application`. They differ in what sits
in front of it.

| | Development (`make up`) | Production-like (`make up-prod`) |
| --- | --- | --- |
| Server | one `uvicorn` process | `gunicorn` arbiter + N `uvicorn` workers |
| Reload | `--reload`, watching the bind mount | none — the image is run as built |
| Source | mounted from your working copy | image contents only |
| Configured by | the `command:` in `docker-compose.yml` | `config/gunicorn.py`, via the image's `CMD` |
| Port | 8000 | 8001 |

Neither profile runs Django's `runserver`. It is single-threaded, unoptimised, WSGI-only, and
explicitly not for production — and because it is WSGI-only, using it in development would have left
the ASGI entrypoint untested until the first async endpoint. Serving both profiles through ASGI is
what keeps that path exercised daily rather than discovered on deploy.

**`app-prod` deliberately has no `command:` override.** It runs the image's own `CMD`. The image is
the deployment artefact, and an override in Compose would mean the thing verified locally is not the
thing that ships — the same reasoning that keeps the source mount out of that service.

### Why gunicorn is not used in development

An arbiter has no `--reload` worth having, and it forks: a traceback arrives from a worker process,
interleaved with the arbiter's restart chatter, instead of on the terminal you are watching.
Development optimises for the edit-run loop; the production-like profile optimises for fidelity.

---

## Why two layers

Gunicorn and uvicorn do different jobs, and the combination exists because neither does the other's.

**Gunicorn supervises processes.** It forks N workers, replaces one that dies, restarts them on
demand, and coordinates shutdown across all of them. It does not speak ASGI at all.

**Uvicorn speaks ASGI.** With the `[standard]` extra it brings uvloop and httptools — a faster event
loop and HTTP parser. On its own it has no meaningful process supervision.

Rejected alongside them: `runserver`, for the reasons above, and **bare uvicorn**, which is what
`app-prod` ran between M3-04 and M6-02. It works, and a single process has nothing watching it: a
worker that dies stays dead, and there is no rolling-restart story.

That supervision is not theoretical. In the timeout measurement below, gunicorn killed a wedged
worker and booted a replacement three lines later:

```
[1]  [CRITICAL] WORKER TIMEOUT (pid:9)
[1]  [WARNING]  Worker (pid:9) was sent SIGABRT!
[32] [INFO]     Booting worker with pid: 32
```

Under bare uvicorn that process would simply be gone.

### The worker class is a separate package, on purpose

`GUNICORN_WORKER_CLASS` defaults to **`uvicorn_worker.UvicornWorker`**, from the standalone
`uvicorn-worker` distribution — not `uvicorn.workers.UvicornWorker`.

The bundled module still exists and still works, and importing it emits:

```
DeprecationWarning: The `uvicorn.workers` module is deprecated.
Please use `uvicorn-worker` package instead.
```

It is slated for removal. A template must not ship a deprecated import that every project forged
from it inherits, so `uvicorn-worker` is a runtime dependency.

---

## Worker count

`GUNICORN_WORKERS`, falling back to `WEB_CONCURRENCY` (the de-facto name platforms set for you), and
computed when neither is set:

```
min((2 × CPUs) + 1, 8)
```

The `(2 × CPUs) + 1` half is the conventional starting point. It assumes each worker spends a
meaningful share of its time blocked on I/O, so the odd extra one has something to do. It is a
reasonable first guess and a poor final answer — see the arithmetic in the next section for what
actually binds.

### `os.cpu_count()` is the wrong number in a container

This is measured, not asserted. On the 12-core machine this was built on:

```
$ docker run --rm --cpus=2 <image> python -c "import config.gunicorn as g; print(g.detect_cpus())"
cpu.max: 200000 100000
detected cpus: 2
os.cpu_count() would say: 12
```

`os.cpu_count()` reports the **host's** cores, not the container's allocation. Naively applied, the
formula would compute `(2 × 12) + 1 = 25` workers for a two-CPU container — 25 processes competing
for two cores, each holding its own database connections.

`detect_cpus()` therefore reads, in order:

| Source | Catches | Note |
| --- | --- | --- |
| `/sys/fs/cgroup/cpu.max` | `--cpus`, Kubernetes CPU limits | cgroup v2. `"200000 100000"` is 2 CPUs; `"max 100000"` means unlimited |
| `/sys/fs/cgroup/cpu/cpu.cfs_quota_us` | the same, on cgroup v1 | two files instead of one |
| `os.sched_getaffinity(0)` | `--cpuset-cpus`, `taskset` | verified: `--cpuset-cpus=0-2` yields 3 |
| `os.cpu_count()` | everything else | the last resort, not the first |

Every step is load-bearing. `--cpus` sets a CFS quota and is invisible to `sched_getaffinity`;
`--cpuset-cpus` restricts affinity and leaves `cpu.max` reading `max`. Neither subsumes the other,
and the machine this was built on has no `/sys/fs/cgroup/cpu.max` at all.

### Why the default is capped at 8

The cap applies **only to the computed default**. An explicit `GUNICORN_WORKERS` or
`WEB_CONCURRENCY` is never capped — a deployment that has sized itself has earned the right to.

It exists because an unconfigured container on a large host computed 25 workers, and 25 workers is
several times PostgreSQL's default `max_connections` once each one is busy. That would be an outage
on first deploy, from a default nobody chose, on a host nobody was thinking about.

Eight is a compromise and not a law: large enough not to throttle a container on a modest host,
small enough that the default alone cannot exhaust an unmodified PostgreSQL. **It is not a safe
ceiling.** Size it deliberately, using the arithmetic below.

---

## Workers and database connections

> ⚠️ **This is the section that prevents an outage.**

```
total database connections ≈ workers × threads per worker
```

Not workers alone. Each worker is a separate **process** holding its own connections, and under ASGI
sync views run in a threadpool where Django connections are **thread-local** — so each busy thread
holds one too.

Measured on this stack with `CONN_MAX_AGE=60` (recorded in
[`config/settings/base.py`](../config/settings/base.py)): **20 concurrent requests to a single
uvicorn process held 20 connections**, against PostgreSQL's default `max_connections` of 100.

Multiply that by the worker count before raising it. Past `max_connections`, PostgreSQL refuses new
connections outright, and the symptom looks nothing like "too many workers" — which is why raising
workers to handle load is the most common way a well-intentioned change takes a service down.

The two halves live in different files and must be read together:

| Half | Where | Variable |
| --- | --- | --- |
| Worker count | `config/gunicorn.py` | `GUNICORN_WORKERS` |
| Connection lifetime | `config/settings/base.py` | `DJANGO_CONN_MAX_AGE` |

At scale the answer is **PgBouncer**, not a larger `max_connections`: each connection costs memory
server-side, so raising the limit moves the failure rather than removing it.

`preload_app` is explicitly `False` for a related reason. Preloading looks like a free memory saving
and is not: Django can open a database connection during startup, a connection created before the
fork is inherited by every worker, and several processes reading and writing the same socket corrupt
each other's results in ways that surface as impossible query errors.

---

## Timeouts

### `GUNICORN_TIMEOUT` is not a request timeout

It is the single most misread gunicorn setting. It is how long the arbiter waits for a worker's
**heartbeat** before deciding the worker is wedged and killing it.

Under an async worker the heartbeat is sent from an asyncio callback on a timer, completely
independently of whether any request is progressing. Both halves were measured with
`GUNICORN_TIMEOUT=5` against a deliberately slow endpoint:

| Endpoint | What it does | Result |
| --- | --- | --- |
| `await asyncio.sleep(60)` | yields the event loop | **HTTP 200 after 60.03s** — worker untouched |
| `time.sleep(60)` in an `async def` | blocks the event loop | **killed at 4.6s**, `WORKER TIMEOUT`, `SIGABRT`, replaced |

So a slow request is never interrupted by it, and an operator who believes this setting is a request
deadline has no request deadline at all.

**What it does catch, and why it is kept:** a worker whose event loop is blocked — synchronous
CPU-bound work inside an `async def`, the classic async-Django mistake. Nothing else in the stack
notices that. See [docs/layout.md](layout.md) on why async Django is not "Django but faster".

A real request deadline belongs at the proxy in front (nginx `proxy_read_timeout`, an ALB idle
timeout) or in the application itself.

### `GUNICORN_GRACEFUL_TIMEOUT` defaults to 25, not 30

How long a worker gets to finish in-flight requests after `SIGTERM` before it is killed.

Gunicorn's own default is 30. **25 is deliberate, and the five seconds are the point:** this must
expire *before* the platform's kill grace period, or the platform `SIGKILL`s mid-drain and the
graceful timeout never gets to do its job. Thirty seconds is precisely the common platform default —
`stop_grace_period` for `app-prod` in `docker-compose.yml`, and Kubernetes'
`terminationGracePeriodSeconds`.

Equal values race. Raise this only together with the platform's grace period, keeping the gap.

---

## Logging

Both streams go to stdout/stderr, never to a file: a container's filesystem is ephemeral and the
platform collects its streams, so a log file inside a container is written, rotated by nobody, and
lost on replacement. `PYTHONUNBUFFERED=1` in the Dockerfile keeps the lines flowing rather than
sitting in a buffer until the process is killed.

**`access_log_format` is deliberately absent, and its absence is a finding.** Under an ASGI worker
gunicorn does not write the access line — uvicorn does. The worker class hands uvicorn gunicorn's
*handlers* and then lets uvicorn's own logger format the record, so gunicorn's `access_log_format` is
never consulted. Measured: with a format string specifying request duration and a correlation
header, the emitted line was still uvicorn's plain

```
172.30.0.1:42090 - "GET /api/v1/ping HTTP/1.1" 200
```

Shipping a setting that silently does nothing is worse than not having it — someone would eventually
rely on a field that was never going to appear.

`accesslog` itself **is** load-bearing, and that was measured too: pointing it at `/dev/null`
silences the access line entirely. It is the switch; it just is not the formatter.

---

## Configuration reaches the container through a file, not your shell

Compose forwards only `env_file` and the keys named in a service's `environment:`. A `GUNICORN_*`
exported in your shell **does not reach the container**.

Verified the hard way: an exported `GUNICORN_WORKERS=3` produced 25 workers. Put it in `.env`, which
both services already read:

```bash
echo 'GUNICORN_WORKERS=3' >> .env
make up-prod
make logs | grep -c "Booting worker"   # 3
```

---

## What this document still owes

| Owed by | What lands here |
| --- | --- |
| **M6-01** | The liveness and readiness endpoints, and the container `HEALTHCHECK` that calls readiness instead of `import django` |
| **M6-03** | Static and media files — `collectstatic` at build time, and why a container filesystem is the wrong place for uploads |
| **M6-04** | Access lines and application logs as JSON through one configuration, with a correlation identifier. Request duration and that identifier have to come from Django middleware, not a server format string — see the logging section above |
| **M6-05** | The shutdown sequence end to end: `SIGTERM` to the arbiter, readiness failing immediately, in-flight requests draining inside `graceful_timeout`, connections closed cleanly |
