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
- **M6-04** — structured logging and request correlation identifiers
- **M6-05** — graceful shutdown and the `SIGTERM` sequence

---

## See also

- [layout.md](layout.md) — settings layers, ASGI, and what belongs where
- [api.md](api.md) — why `/api/v1/ping` is not a health check
- [migrations.md](migrations.md) — why migrations do not run at container startup
