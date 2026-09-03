# Running this service

What a platform needs to know about the application, and what the application needs to know about
the platform. This document grows through M6; [what is still owed](#what-m6-still-owes-this-document)
is listed at the bottom.

```bash
make up
curl localhost:8000/healthz   # {"status": "alive"}
curl localhost:8000/readyz    # {"status": "ready"}
```

---

## Liveness and readiness are two different questions

An orchestrator asks a service two things. They sound alike and they are not:

| | The question | The action it drives | Endpoint |
| --- | --- | --- | --- |
| **Liveness** | Is this process healthy? | Kill and restart it | `/healthz` |
| **Readiness** | Should this process get traffic? | Add to, or remove from, the load balancer | `/readyz` |

A container still opening its database connections is **alive but not ready**. It should keep
running, and it should not receive requests yet. One endpoint cannot express that.

**The failure that follows from conflating them** is the reason this is two endpoints and not one.
Point a `livenessProbe` at a check that touches the database, and a thirty-second database blip
becomes a restart of *every replica simultaneously* — during which no replica can serve, the
database is hit by a thundering herd of reconnecting processes, and the outage outlives its cause.
The service was never unhealthy. It was told it was.

So: **liveness touches nothing.** Not the database, not a cache, not a queue. It answers whether
the process can route a request through the middleware stack and return a response, because
restarting is the correct remedy for exactly one condition — a process that cannot do that.

### What readiness checks

`apps/core/health.py` holds a table:

```python
READINESS_CHECKS = (
    ("database", _database_is_reachable),
)
```

One `SELECT 1` against the default database, which is a real round trip and not just
`ensure_connection()` — with `CONN_MAX_AGE=60` a pooled connection can be open and dead at the same
time, killed by a database restart or a network blip, and only a round trip catches that.

**Hard dependencies only.** This is the rule that keeps readiness from causing the outage it exists
to prevent. A readiness check that verifies a non-essential third-party service pulls the *entire*
service out of rotation because of someone else's downtime — and nothing you can do will bring it
back until they recover.

The test a new entry has to pass: *if this dependency is down, is the service genuinely unable to
serve any request?* If some requests would still succeed, it does not belong here. Degrade that
feature instead.

Keep each check cheap. It runs every few seconds, for the life of every container, forever.

### Neither endpoint says anything

```
GET /healthz  ->  200  {"status": "alive"}
GET /readyz   ->  200  {"status": "ready"}
              ->  503  {"status": "not ready"}
```

That is the whole response. Which check failed, and why, goes to the **log**:

```
readiness: database check failed: connection to server at "db" (172.18.0.2), port 5432 failed
readiness: not ready (database)
```

A probe cannot hold credentials, so both endpoints are unauthenticated by necessity, and whatever
they return is returned to anything that can reach the port. "The database is unreachable at
db:5432" is free reconnaissance for someone mapping your infrastructure. The operator gets the
detail; the caller gets a status code.

503, not 500: this is a *temporary* inability to serve, which is what 503 means and what load
balancers act on.

---

## The URLs are deliberately not under `/api/v1/`

`/api/v1/ping` exists and is **not** a health check — it answers for one thing, that routing reached
django-ninja. See [api.md](api.md).

The probes sit at the root instead, and stay there:

- A probe URL is an **infrastructure contract**. It lives in a `Dockerfile`, a Compose file, a
  Kubernetes manifest, a load balancer's configuration — none of which have any opinion about API
  versions. The `/api/v1/` prefix is a contract boundary for API *clients*, and a future v2 must not
  be able to move a URL that deployments depend on, for a reason unrelated to the API changing.
- They keep answering when the `NinjaAPI` instance fails to build, which is precisely the moment a
  probe earns its keep.
- They stay out of `openapi.json`, so nobody generates a client against them.

The paths are written down once, in `HEALTH_CHECK_PATHS` (`config/settings/base.py`). The URLconf,
the SSL-redirect exemption and the log filter all derive from that tuple, so they cannot drift.

---

## Wiring it to a platform

### Kubernetes

```yaml
livenessProbe:
  httpGet: { path: /healthz, port: 8000 }
  initialDelaySeconds: 10
  periodSeconds: 10
  timeoutSeconds: 3           # set it; the probe hanging is a failure mode of its own
readinessProbe:
  httpGet: { path: /readyz, port: 8000 }
  periodSeconds: 5
  timeoutSeconds: 3

terminationGracePeriodSeconds: 30   # must exceed GUNICORN_GRACEFUL_TIMEOUT (25)
```

> ⚠️ **Do not point `livenessProbe` at `/readyz`.** It is the single most common way this gets
> wired, it passes every test in staging, and the first real database blip restart-loops the entire
> deployment.

**`ALLOWED_HOSTS` and the pod IP.** A kubelet probe connects to the pod IP and sends it as the
`Host` header, so Django rejects it with `400 DisallowedHost` unless that IP is allowed. The
production layer already appends `localhost` and `127.0.0.1` (see below); for the pod IP, either
add it from the downward API:

```yaml
env:
  - name: POD_IP
    valueFrom: { fieldRef: { fieldPath: status.podIP } }
```

and append `$(POD_IP)` to `DJANGO_ALLOWED_HOSTS`, or set `httpGet.httpHeaders` to send a `Host` you
already allow.

### Docker and Compose

The image's `HEALTHCHECK` requests **`/readyz`**, because `healthy` is what other services gate on
with `condition: service_healthy`, and that question is "can it serve", not "is it alive".

That is safe *with Docker specifically*: Docker never restarts a container for being unhealthy. A
database outage marks the app unhealthy, leaves it running, and it recovers by itself. Under an
orchestrator the two questions get two probes, as above.

The probe is Python, not `curl` — the slim image has neither `curl` nor `wget`, and adding a package
to run a health check is a package and an attack surface for something the interpreter already does.
It uses `http.client` rather than `urllib.request` because that returns a non-2xx response instead
of raising, so a 503 exits `1` cleanly rather than through a traceback.

---

## Two things that break probes in production, and are already handled

Both produce the same symptom — a perfectly healthy application that the platform reports as
unhealthy — and neither points anywhere near its cause.

**The HTTPS redirect.** The production layer sets `SECURE_SSL_REDIRECT=True`, and the probe reaches
the application over plain HTTP from inside the container. Without an exemption it receives a `301`
to `https://`, never a `200`, and the container is unhealthy forever. `SECURE_REDIRECT_EXEMPT` in
`config/settings/base.py` covers exactly the two probe paths, anchored so nothing else is served
over plaintext.

**Host validation.** The probe connects to `127.0.0.1` and cannot know the public hostname, so a
strict `ALLOWED_HOSTS` rejects it with `400 DisallowedHost`. The production layer appends
`localhost` and `127.0.0.1`.

That widening is narrow on purpose. Host-header validation exists to stop an attacker poisoning the
absolute URLs Django builds — password-reset links, redirects. A forged `Host: localhost` produces a
link pointing at the victim's own loopback, which is worth nothing to the attacker. Naming two
loopback values is a different thing entirely from `ALLOWED_HOSTS = ["*"]`, which is what it must
never be "simplified" into.

---

## Probes are excluded from request logging

Polled every few seconds, for the life of every container, from every replica, the probes would be
the overwhelming majority of the log — and expensive, once logs are shipped somewhere billed by
volume.

Two loggers emit a line per request, and only one of them is obvious:

- `uvicorn.access` — one access line per request
- `django.request` — every 4xx and 5xx, which during a database outage means *every readiness
  probe*, at `ERROR`, from every replica, for the duration

`SuppressHealthCheckAccessLogs` (`config/logging.py`) filters both, and **fails open**: a record
carrying an exception is always kept, and so is one whose path it cannot identify. A filter that
silently swallows real request logs is far worse than one that occasionally logs a probe — and the
exception carve-out is what makes sure a genuine crash *inside* a probe view still reaches the log
with its traceback.

The probes' own diagnostic logging is untouched. `apps/core/health.py` logs the failing check and
the underlying error message every time readiness fails — the message, not `exc_info`: a chained
traceback for an unreachable database is about sixty lines through the ORM's internals, repeated on
every probe for the length of the outage, which is the same flood in a different costume. The
message alone is what is actionable.

> **When M6-02 lands**, gunicorn brings its own access log through `gunicorn.access`. The same
> filter has to be pointed at it, or the probes reappear.

---

## Shutting down without dropping requests

A container is stopped on every deploy, every scale event and every node replacement. The platform
sends `SIGTERM`, waits a grace period, then `SIGKILL`s. An application that ignores `SIGTERM` loses
every request it was serving, on every release — errors that correlate with deploys and are hard to
attribute, because the process that would have logged them was killed.

The sequence, in the order it happens:

| # | What happens | Who does it |
| --- | --- | --- |
| 1 | `SIGTERM` arrives at the server, as PID 1 | the platform |
| 2 | readiness starts answering **503** | `apps/core/shutdown.py` |
| 3 | the listening socket closes — no new connections | uvicorn / the gunicorn arbiter |
| 4 | in-flight requests run to completion | uvicorn, bounded by `graceful_timeout` |
| 5 | database connections are closed | `apps/core/shutdown.py`, on ASGI lifespan shutdown |
| 6 | the process exits, inside the platform's grace period | uvicorn / gunicorn |

Steps 1, 3, 4 and 6 are the servers doing their job. Steps 2 and 5 are this project's, and both are
in `apps/core/shutdown.py`.

**Nothing may sit between the platform and the server.** `docker-entrypoint.sh` ends with
`exec "$@"`, which *replaces* the shell rather than spawning under it, so the server is PID 1 and
receives the signal directly. The `Dockerfile`'s `ENTRYPOINT` and `CMD` are both exec form for the
same reason — shell form would reintroduce an `sh -c` one line later. Without that, the shell holds
the signal, the platform waits out the entire grace period, and then `SIGKILL`s: every in-flight
request lost, having taken the full 30 seconds to lose them.

### The timeouts have to be ordered, not just set

```
   uvicorn --timeout-graceful-shutdown  25s  ── development
   GUNICORN_GRACEFUL_TIMEOUT            25s  ── production
                                         <
   stop_grace_period / terminationGracePeriodSeconds   30s
```

The gap is the point. If the graceful timeout is equal to or larger than the platform's grace
period, the platform `SIGKILL`s while the drain is still running — the drain is configured, looks
configured, and never completes. Raise one and you raise the other, keeping the headroom.

Uvicorn's own default is to wait **forever** for in-flight requests, which under a platform grace
period means "wait until killed". Both profiles therefore set it explicitly, and to the same number,
so a shutdown bug shows up on a laptop rather than on a deploy.

### Liveness stays 200 the whole time, deliberately

A draining process is *healthy*. It is finishing its work and leaving.

Failing liveness during a drain tells the platform to **kill** it, which produces exactly the
dropped requests this whole mechanism exists to prevent — arriving by way of the mechanism meant to
prevent them. Readiness is what changes during a shutdown. Liveness is not.

> ⚠️ Under Docker the container will show `unhealthy` while draining, because the image's
> `HEALTHCHECK` requests `/readyz`. That is correct and harmless: Docker never restarts a container
> for being unhealthy, and the container is about to exit anyway.

### What the readiness flip actually buys, and what it does not

Step 2 happens the instant the signal lands, and step 3 happens almost as quickly — uvicorn closes
the listening socket at the top of its own shutdown. So an external probe opening a **new**
connection during the drain will usually see the connection refused rather than a 503.

Both answers mean "not taking new traffic", which is what matters. But it is worth being precise
about which mechanism is doing the work:

- **The closed socket** is what stops new connections. It needs no help.
- **The 503** is what protects requests arriving on connections that are *already open* — keep-alive
  connections a load balancer is holding — and is what a health-check-driven balancer polls to
  decide to deregister.

What neither closes is the window between the platform deciding to stop the container and its load
balancer noticing. Endpoint removal is asynchronous almost everywhere, so a request can be
dispatched to a process that has already been signalled. The in-process answer to that window does not exist;
the platform-side one is a pre-stop delay that keeps the process serving while the balancer catches
up. On Kubernetes that is a `preStop` hook, and its sleep has to fit inside
`terminationGracePeriodSeconds` alongside the drain.

### Verifying it

```bash
make shutdown-demo
```

Starts a real server, holds a request open, sends it `SIGTERM` mid-request, and asserts the response
still arrives complete. It is the same test `make check` runs — a demonstration that is also a
regression test, rather than a second script that drifts from one.

Against the production-like stack, which is where gunicorn's arbiter is actually involved:

```bash
make up-prod
curl "localhost:8001/api/v1/ping"                        # serving
docker compose --profile prod stop app-prod              # watch `make logs`
```

The log carries the drain: `shutdown: SIGTERM received, draining`, then uvicorn's `Shutting down`,
then `shutdown: database connections closed`. The container must exit well inside 30s — if Compose
reports it after the full grace period, something is holding the signal.

**One thing deliberately left alone.** `docker-entrypoint.sh` installs no `trap` around its
database-wait loop. During that wait the shell *is* PID 1, and bash's default disposition ends it
between commands, so a container stopped mid-startup already exits within one connect attempt. A
trap would add a failure path to the one part of startup that has no requests to protect.

---

## What this does not solve

**A hung dependency, as opposed to a refused one.** A database that accepts the connection and never
answers makes `/readyz` hang rather than fail, until the platform's probe timeout fires. That still
counts as a failed probe, which is the right outcome — but only if a timeout is actually configured.
Docker's `--timeout` is set in the `Dockerfile`; Kubernetes' `timeoutSeconds` defaults to 1s and is
easy to leave unset in a manifest that never gets reviewed under load.

**Startup versus steady state.** These two probes cover "is it alive" and "can it serve". Kubernetes
also offers a `startupProbe` for applications with a slow, variable boot, which suppresses the other
two until it passes. This one boots quickly enough not to need it; a derived project that adds
expensive startup work should reach for it rather than inflating `initialDelaySeconds`.

---

## What M6 still owes this document

Each arrives with its issue, and this file is where it gets written down:

- **M6-02** — the production application server, its worker and timeout defaults, and the reasoning
- **M6-03** — the static and media file strategy
- **M6-04** — structured logging and request correlation identifiers

---

## See also

- [layout.md](layout.md) — settings layers, ASGI, and what belongs where
- [api.md](api.md) — why `/api/v1/ping` is not a health check
- [migrations.md](migrations.md) — why migrations do not run at container startup
