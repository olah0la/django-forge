# Logging

Two formats, one configuration, and one identifier that ties a request's log lines together.

**JSON in production, prose in development.** A log collector wants machine-parseable records; a
developer wants to read them. The first time someone needs *every failed request for this user in
this window*, structured records make it a query and prose makes it guesswork.

```console
# development
13:44:42 INFO     uvicorn.error [-] Application startup complete.
13:44:47 INFO     apps.request [01a0625d-7494-742b-84af-e8652668a71e] GET /api/v1/ping 200
13:44:47 INFO     apps.request [01a0625d-74aa-7eea-821e-880e512a5f1f] GET /nope 404
13:44:47 WARNING  django.request [01a0625d-74aa-7eea-821e-880e512a5f1f] Not Found: /nope
```

```console
# production — one object per line, wrapped here for the page
{"timestamp": "2026-09-02T13:44:26.352Z", "level": "INFO", "logger": "apps.request",
 "message": "GET /nope 404", "request_id": "01a0625d-23e4-7703-92a8-148a90d2e17f",
 "method": "GET", "path": "/nope", "status": 404, "duration_ms": 12.14,
 "module": "middleware", "line": 174}
{"timestamp": "2026-09-02T13:44:26.353Z", "level": "WARNING", "logger": "django.request",
 "message": "Not Found: /nope", "request_id": "01a0625d-23e4-7703-92a8-148a90d2e17f",
 "status_code": 404, "module": "log", "line": 253}
```

Same request, two loggers, one identifier — and uvicorn's own startup output goes through the same
formatter rather than out in a shape of its own.

---

## Configuration

| Variable | Default | Effect |
| --- | --- | --- |
| `DJANGO_LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR` or `CRITICAL`. Case-insensitive. |
| `DJANGO_LOG_FORMAT` | `json` in production, `console` in development | Anything else refuses to start. |

Everything is assembled by `build_logging()` in `config/settings/base.py`, which each layer calls
with the format it wants. The pieces it references — the formatter, the filter, the redaction
helpers — live in `apps/core/logging.py`.

**Everything goes to stdout, and nothing is ever written to a file.** A log file inside a container
is deleted with the container, and the one occasion you want it is after the container is gone.

### Preview the production format on your laptop

Running two formats has a cost, and it is worth naming: the format that actually matters is the one
nobody looks at until it is deployed, so a formatting bug can hide until an incident. This is the
mitigation, and it takes one variable:

```bash
DJANGO_LOG_FORMAT=json make up
```

Do it before shipping a change that adds a log call with unusual data in it.

---

## The correlation identifier

A unique value attached to every log line produced while handling one request. Without it, a
worker serving many requests at once produces lines that cannot be reassembled into the request
that caused them.

`RequestIDMiddleware` (`apps/core/middleware.py`) puts it in a `ContextVar`, and a logging *filter*
on the handler copies it onto every record on its way out. **You do not have to pass it anywhere.**

```python
import logging

logger = logging.getLogger(__name__)


def do_something(user):
    # This line is correlated with whatever request is in flight. Nothing to wire up.
    logger.info("granting access to %s", user.pk)
```

Lines produced outside a request — startup, a management command — carry `-`.

### The header contract

The identifier is read from **`X-Request-ID`** and returned in the same header on every response,
including error responses.

* **Present and usable** → it is used as-is. This is what lets one request be followed across two
  services: the caller passes on the identifier it was given, and the two services' logs join on it.
* **Absent or malformed** → a fresh UUIDv7 is generated. The request is *not* rejected; a bad header
  must never be the reason a request failed.

Usable means `[A-Za-z0-9._-]`, between 1 and 128 characters. Both halves of that matter:

* **A newline in the header forges a log line.** An unvalidated value is attacker-controlled text
  written straight into the stream, where a parser reads the second half as a separate record. Note
  that the anchor in the pattern is `\Z` and not `$` — in Python `$` also matches immediately before
  a trailing newline, so `^[\w-]+$` accepts `"abc\n"`.
* **The length cap** bounds a value that is held for the whole request and copied into every line it
  produces.

The header name is `REQUEST_ID_HEADER` in `config/settings/base.py` and is deliberately **not**
environment-readable: it is a contract between this service and its callers, and changing it on one
side only makes correlation stop working with nothing failing.

### Why the identifier also rides on the request object

A context variable alone does not cover everything, and this was measured rather than predicted.

Django logs 4xx and 5xx responses from `BaseHandler.get_response`, which runs **after**
`_middleware_chain` has returned — outside every middleware, including the one that sets the
identifier, and under ASGI in a different thread. Before the fallback existed, a single 404 produced
this, one millisecond apart:

```console
{"logger": "apps.request",   "message": "GET /nope 404",   "request_id": "01a0625b-a4d3-…"}
{"logger": "django.request", "message": "Not Found: /nope", "request_id": "-"}
```

A **500** was correlated anyway, because an exception is logged by `response_for_exception` from
*inside* the chain — which is exactly what made the gap easy to miss. A plain 4xx (a 404, a 401, one
of Ninja's 422s) is not.

So the middleware also sets `request.request_id`, and `RequestIDFilter` falls back to reading it off
`record.request` when the context variable is empty — those are precisely the records Django emits
outside the chain, and they carry the request. Only the attribute is read; the request object itself
is never serialised, for the reason in the next section.

---

## The request log

`RequestIDMiddleware` emits exactly one line per request, on the `apps.request` logger:

| Field | Example |
| --- | --- |
| `method` | `POST` |
| `path` | `/api/v1/logging/boom` |
| `status` | `500` |
| `duration_ms` | `49.25` |

**This is the request log.** Uvicorn's access log is silenced in `build_logging()`, and Gunicorn's
should be too when M6-02 adds it, for one reason: the application middleware is the only layer that
can see the correlation identifier. Two lines per request, one of them uncorrelated, is worse than
one line that is.

The middleware is **first** in `MIDDLEWARE`. Middleware wraps in list order, so the first entry is
the outermost: the identifier exists before anything else runs, and the response header is attached
after everything else has finished. Moved further down, every line logged above it is uncorrelated —
including the lines from a request rejected before it ever reached a view, which are the ones you
most want to be able to find.

### Excluding noisy paths

`REQUEST_LOG_EXCLUDED_PATHS` in `config/settings/base.py` lists paths that get an identifier and a
response header but no log line. It **ships empty**, with a `TODO(M6-01)`: an orchestrator probes
liveness and readiness every few seconds for the life of every container, which is tens of thousands
of identical lines a day burying everything of interest. M6-01 owns those endpoints' paths and fills
this in.

---

## What is never logged, and how

Four controls, and each one is something that *cannot* leak rather than a filter hoping to catch a
leak. Verified end to end by `tests/test_logging.py` — a request carrying a password in its body and
a token in its query string, to an endpoint that then fails.

**1. The JSON formatter publishes an allow-list, not "every extra".**
This is the one that is easy to get wrong, and getting it wrong is the default. Django's
`django.request` logger attaches the live `HttpRequest` to the records it emits for a 4xx or 5xx, as
`record.request`. Its repr is:

```
<ASGIRequest: POST '/api/v1/things?token=s3cret'>
```

A formatter that serialises unknown record attributes — the obvious implementation, and what the
JSON logging libraries do out of the box — writes that query string into the log. `JSONFormatter`
publishes only the fields it knows about, so `record.request` is dropped, and so is whatever a
future dependency starts attaching that nobody has thought about yet.

**2. The request line logs `request.path`, never `request.get_full_path()`.**
Query strings hold password-reset codes, invite tokens and signed URLs. A scrubber that tries to
clean them is a scrubber that eventually meets a parameter name nobody added to the list. Not
logging them cannot miss.

**3. `django.db.backends` is pinned at `INFO` and does not follow `DJANGO_LOG_LEVEL`.**
At `DEBUG` it prints every SQL statement **with its bound parameters** — every password hash, token
and personal detail the application has ever written to a row. This is the widest leak available in
a logging configuration and it is one environment variable away in most projects. Turning it on is a
deliberate local edit and should never be done against production data:

```python
# config/settings/local.py, temporarily, against a development database
LOGGING["loggers"]["django.db.backends"]["level"] = "DEBUG"
```

**4. `DEBUG` is `False` in production and cannot be turned on.** See `config/settings/production.py`.

### What these do *not* cover

Be honest about the edges:

* **`@sensitive_variables` and `@sensitive_post_parameters`** govern Django's debug page and
  `AdminEmailHandler`. They do not filter anything on this path. Use them anyway — they are correct
  for what they cover — but do not read them as protecting the log.
* **Anything you pass to `extra=` yourself** is published if its key is in the allow-list and
  dropped if it is not. If you must log a dictionary that might hold a secret, run it through
  `redact_mapping()` from `apps/core/logging.py`, which masks values whose key *looks* sensitive.
  It is a heuristic and cannot see a secret stored under an innocent name.
* **Third-party libraries log whatever they like.** They reach the same handler, and the allow-list
  keeps their extras out of the output, but their *messages* are their own.

---

## Adding a logger

Use the module's own name and let it propagate. The root logger owns the only handler, so a logger
that adds one *and* propagates prints every line twice.

```python
logger = logging.getLogger(__name__)      # yes
logger = logging.getLogger("apps.thing")  # also fine — `apps` is configured in base.py
```

---

## Two things not to "fix"

**`disable_existing_loggers: False` is load-bearing.** `dictConfig` defaults it to `True`, which
switches off every logger created by an import that already ran — most of Django's, and every
dependency's. Nothing fails; the lines simply stop.

**`apps/core/logging.py` may import nothing but the standard library.** Django calls
`configure_logging()` *before* `apps.populate()`, so `dictConfig` imports that module while the app
registry is still empty. An import that reaches a model raises `AppRegistryNotReady` at startup,
with a traceback pointing at the logging configuration rather than at the import that caused it.

---

## See also

* [layout.md](layout.md) — where code belongs, and the settings layering this configuration follows
* [api.md](api.md) — the API surface the request log describes
* `config/settings/base.py` — `build_logging()`, and the reasoning beside each logger
