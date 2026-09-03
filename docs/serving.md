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

## Static files

Static files are **collected into the image at build time and served by WhiteNoise**, in every
profile. There is nothing to configure and nothing to run before a deploy.

```bash
make build
docker compose --profile prod run --rm --entrypoint sh app-prod -c 'ls staticfiles/ | head'
# admin  ninja  staticfiles.json

make up-prod
curl -sI localhost:8001/static/admin/css/base.96c479cedf7a.css | grep -i cache-control
# cache-control: max-age=315360000, public, immutable
```

### Collected during the build, not at startup

The `collectstatic` step lives in the `builder` stage of the `Dockerfile`, next to the source copy.
This is the same argument [migrations.md](migrations.md) makes for not migrating at startup, reached
for a different reason: an image that collects its own assets is **self-contained**, and its contents
depend on what it is rather than on when it happened to boot.

Collecting at container start would instead redo identical work on every replica of every deploy,
and would put a filesystem walk inside the window the health check is timing.

**The cost, stated honestly.** A changed asset now requires a rebuild — you cannot edit a stylesheet
in a running production container and see it — and the image is larger by the size of the collected
tree. Measured on this repository: 136 source files collected and 676 post-processed, landing as 270
`.gz` and 270 `.br` siblings beside their hashed originals — most of it the Swagger UI bundle that
renders `/api/v1/docs`.

### The build step uses dummy environment variables, and that is fine

`collectstatic` runs under `config.settings.production`, which requires a secret key, allowed hosts
and a database URL. The Dockerfile supplies throwaway values, exactly as `make audit` and
`make typecheck` already do:

```dockerfile
RUN DJANGO_SETTINGS_MODULE=config.settings.production \
    DJANGO_ENV_FILE=/nonexistent \
    DJANGO_ALLOWED_HOSTS=build.invalid \
    DATABASE_URL=postgresql://build:build@localhost:5432/build \
    DJANGO_SECRET_KEY="$(/opt/venv/bin/python -c 'import secrets; print(secrets.token_urlsafe(50))')" \
    /opt/venv/bin/python manage.py collectstatic --noinput
```

Nothing connects and nothing is served: the command only needs the settings module to **import**.
The key is *generated* rather than written as a literal, so nothing resembling a credential enters
the repository or a layer of the image — `make audit` scans the full git history and must stay clean.

**Production settings, not development, and that part is load-bearing.** Only the production layer
uses the storage backend that writes `staticfiles.json` and the compressed variants. Collect under
development settings and you get an unhashed tree with no manifest, and every `{% static %}` call
raises at runtime.

### Why the application serves them at all

The usual advice is that a Python process should not serve bytes — put nginx or a CDN in front. That
advice is about `django.contrib.staticfiles`, and it is correct about it.

WhiteNoise is a different thing wearing the same shape. Outside development it does no filesystem
work per request at all — the URL-to-file mapping is built once at startup and a lookup is a `dict`
hit — and it answers with three properties the development handler has none of:

| | `ASGIStaticFilesHandler` | WhiteNoise |
| --- | --- | --- |
| Cache headers | none | `max-age=315360000, public, immutable` on hashed names |
| Compression | none | `.gz` and `.br` written at build time, chosen by `Accept-Encoding` |
| Content hashing | none | `base.css` → `base.96c479cedf7a.css` |
| Disk access per request | re-reads | none, outside development |

Content hashing is what makes that lifetime safe: a changed file is a changed **name**, so there is
no stale-asset window to reason about. Without it, a long cache lifetime is precisely how a deploy
ships new HTML against a browser's cached old stylesheet.

The pairing is enforced, not assumed. WhiteNoise decides per response, and an unhashed name does not
get the long header — measured on the running production-like profile:

```
/static/admin/css/base.96c479cedf7a.css   cache-control: max-age=315360000, public, immutable
/static/admin/css/base.css                cache-control: max-age=60, public
```

**Rejected: an nginx sidecar in the prod profile.** It would be more faithful to a large deployment,
and it would add a service, a config file and a second port to a stack that is otherwise two
containers. The template declines to pick an edge on an adopter's behalf for the same reason it
declines to pick a deployment target.

**The cost of this choice** is worker CPU spent on bytes a CDN could serve, and it is the reason the
next section exists.

### Putting a CDN in front

Nothing needs to change in the application. Point the CDN at the service as its origin and it caches
correctly on the first request, because the responses already carry `immutable` and a hash in the
filename — the two things a CDN needs to be told, said in the response rather than in a config file.

Serving assets from a *separate* domain rather than through the same host is the one change that
needs code: `STATIC_URL` is a path (`static/`), and pointing it at `https://cdn.example.com/static/`
makes Django write absolute URLs into every template. That is a one-line override in your own
settings layer when you need it, deliberately not shipped as a variable here — a template that ships
a guess at a CDN hostname is a template every adopter has to un-guess.

---

## Media files are not static files

> ### ⚠️ A container filesystem is not storage
>
> Django's default writes uploads to `MEDIA_ROOT`, **inside the container**. That works perfectly on
> a laptop, works perfectly in staging, and destroys every uploaded file the first time a container
> is replaced — which is every deploy, every scale-in, and every node replacement.
>
> Nothing raises. No log line appears. The files are simply gone, and the code that lost them still
> passes its tests. This is the single most expensive default in a containerised Django project,
> because the feedback arrives weeks after the mistake.

Static files ship *with* the code and change when you deploy. Media files arrive *at run time, from
people*, and must outlive every container that ever handles them. Django names the two settings
almost identically and in development both are just files on disk, which is why they get conflated.

### The substitution point

The answer is **object storage** — S3, GCS, Azure Blob, or whatever the platform offers. It is
durable independently of any container, reachable from every replica at once, and it is what every
managed platform expects you to be using.

`STORAGES["default"]` reads a dotted path from the environment, so getting there is a variable
rather than a patch:

```bash
uv add django-storages[s3]
echo 'DJANGO_DEFAULT_FILE_STORAGE=storages.backends.s3.S3Storage' >> .env
```

It chooses **which** backend, not how that backend is configured. Bucket, region, credentials and
signing are the backend's own settings and belong in your settings layer — deliberately not modelled
here, because every provider names them differently and a template that guessed would be wrong for
all but one.

An unimportable path fails loudly at the first file access rather than falling back to local disk.
That is the behaviour to want: a silent fallback is exactly how uploads end up on an ephemeral
filesystem without anyone having decided to put them there.

### Why there is no `media_data` volume

Adding one to `app-prod` would be a single line, and it would look like a fix — uploads would survive
`docker compose down` on your machine. It is deliberately not offered.

A volume is **one host**. The moment there is a second replica, half the uploads are invisible to
half the requests; the moment a node is replaced, they are gone. A volume does not solve the problem,
it moves the discovery of the problem from your laptop to production, which is the wrong direction.

The production-like profile therefore behaves exactly like a deployment: uploads written to the
container are lost when it is replaced. That is not an omission.

### Two rules for code that handles uploads

1. **Go through the storage API**, never `open()` on a path. `default_storage.save(...)`,
   `FileField`, `instance.file.open()`. Code that opens a path works with the filesystem backend and
   breaks the day someone switches to S3 — and it breaks at run time, on the upload path, in
   production.
2. **Never serve media through Django in production.** `config/urls.py` serves `MEDIA_URL` under
   `DEBUG` only, via `static()`, which returns an empty list when `DEBUG` is false. It exists so an
   `ImageField` is viewable locally. WhiteNoise deliberately does not do this job: it serves
   `STATIC_ROOT`, which is code, cacheable for a year, and public.

---

## What this document still owes

| Owed by | What lands here |
| --- | --- |
| **M6-01** | The liveness and readiness endpoints, and the container `HEALTHCHECK` that calls readiness instead of `import django` |
| **M6-04** | Access lines and application logs as JSON through one configuration, with a correlation identifier. Request duration and that identifier have to come from Django middleware, not a server format string — see the logging section above |
| **M6-05** | The shutdown sequence end to end: `SIGTERM` to the arbiter, readiness failing immediately, in-flight requests draining inside `graceful_timeout`, connections closed cleanly |
