"""Gunicorn configuration — how the production-like profile is served.

Read by ``gunicorn -c python:config.gunicorn`` (the ``python:`` prefix loads a
MODULE rather than a file path, so this works from any working directory). The
Dockerfile's ``CMD`` is the only place that names it; ``app-prod`` inherits that
CMD rather than overriding it, so the image is the deployment artefact and this
file is its tuning.

Every module-level name below is a gunicorn setting. Importing this module runs
``config/__init__.py`` and nothing else — no Django setup — which is deliberate:
gunicorn reads its configuration BEFORE it loads the application.

**Two layers, two jobs.** Gunicorn supervises processes: it forks workers,
restarts one that dies, and coordinates shutdown. Uvicorn speaks ASGI. Neither
does the other's job, which is why both are here rather than either alone. See
docs/serving.md.

**The defaults here are a starting point, not a law.** They are inherited by
every project forged from this template, so each one records WHY it is what it
is. Two of them are routinely misunderstood, and both are called out below:
``timeout`` does not bound a request, and ``workers`` multiplies against
PostgreSQL's connection limit.

Development does NOT use gunicorn — see docker-compose.yml. There is no
``--reload`` equivalent worth having under a process arbiter, and worker
supervision buries the traceback you actually wanted to read.
"""

import math
import os
from pathlib import Path

# --------------------------------------------------------------------------
# Environment reading
# --------------------------------------------------------------------------
# NOT django-environ: that lives behind Django's settings machinery, and this
# file is read before Django exists. The one behaviour worth copying from
# config/settings/base.py is treating an EMPTY value as absent — an unset
# template placeholder or a blank CI secret is a common deployment slip, and
# `int("")` raises a ValueError with no clue which variable caused it.


def _env_int(name: str, default: int) -> int:
    """Return an integer environment variable, treating empty as unset."""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer, got {raw!r}") from exc


def _env_str(name: str, default: str) -> str:
    """Return a string environment variable, treating empty as unset."""
    return os.environ.get(name, "").strip() or default


# --------------------------------------------------------------------------
# How many CPUs does this container actually have?
# --------------------------------------------------------------------------
# ⚠️  os.cpu_count() reports the HOST's cores, not the container's allocation.
#
# `docker run --cpus=2` on a 12-core machine leaves os.cpu_count() returning
# 12, so the conventional (2 × cpus) + 1 formula computes 25 workers for a
# 2-CPU allocation — 25 processes competing for two cores, each holding its own
# database connections. Measured on this repository's own image; docs/serving.md
# has the numbers.
#
# `--cpus` is a CFS quota and is visible ONLY in the cgroup files below.
# `--cpuset-cpus` is different: it restricts affinity, which sched_getaffinity
# does see. Both are checked, in that order, because neither subsumes the other.

_CGROUP_V2_CPU_MAX = Path("/sys/fs/cgroup/cpu.max")
_CGROUP_V1_QUOTA = Path("/sys/fs/cgroup/cpu/cpu.cfs_quota_us")
_CGROUP_V1_PERIOD = Path("/sys/fs/cgroup/cpu/cpu.cfs_period_us")


def parse_cgroup_v2_cpu_max(text: str) -> float | None:
    """Parse cgroup v2 ``cpu.max`` into a CPU count.

    The file holds ``"<quota> <period>"`` in microseconds — ``"200000 100000"``
    is two CPUs — or ``"max <period>"`` when no limit is set, which returns
    None so the caller falls through to the next source.
    """
    parts = text.split()
    if len(parts) != 2 or parts[0] == "max":
        return None
    try:
        quota, period = int(parts[0]), int(parts[1])
    except ValueError:
        return None
    if quota <= 0 or period <= 0:
        return None
    return quota / period


def detect_cpus() -> int:
    """Best available answer to "how many CPUs may this process use?".

    Ordered most-specific first, and every step is load-bearing: the machine
    this was built on has no /sys/fs/cgroup/cpu.max at all, so the fallback
    chain is real rather than decorative. Rounded UP, and never below 1 — a
    half-CPU allocation still needs a worker.
    """
    try:
        cpus = parse_cgroup_v2_cpu_max(_CGROUP_V2_CPU_MAX.read_text())
    except OSError:
        cpus = None
    if cpus:
        return max(1, math.ceil(cpus))

    try:
        quota = int(_CGROUP_V1_QUOTA.read_text())
        period = int(_CGROUP_V1_PERIOD.read_text())
        if quota > 0 and period > 0:
            return max(1, math.ceil(quota / period))
    except (OSError, ValueError):
        pass

    # Set by `--cpuset-cpus` and by taskset. Linux only.
    if hasattr(os, "sched_getaffinity"):
        return max(1, len(os.sched_getaffinity(0)))

    return max(1, os.cpu_count() or 1)


# A guardrail on the COMPUTED DEFAULT ONLY. It is not a recommendation, and it
# is emphatically not a safe ceiling — an explicit GUNICORN_WORKERS is never
# capped, because a deployment that has sized itself has earned the right to.
#
# Why it exists: measured on this image, an unconfigured container on the
# 12-core machine this was built on computed (2 × 12) + 1 = 25 workers. Each is
# a separate process holding its own database connections, and base.py's own
# measurement puts a single busy process at 20 connections. Twenty-five of them
# is several times PostgreSQL's default max_connections of 100 — an outage on
# first deploy, from a default nobody chose, on a host nobody was thinking
# about. A template must not ship that.
#
# Eight is a deliberate compromise, not a law: enough that a container on a
# modest host is not artificially throttled, small enough that the default
# cannot on its own exhaust an unmodified PostgreSQL. Size it properly and set
# GUNICORN_WORKERS — docs/serving.md walks the arithmetic through.
DEFAULT_WORKER_CAP = 8


def default_workers() -> int:
    """(2 × CPUs) + 1, capped — the conventional starting point, and only that.

    The formula assumes each worker spends a meaningful share of its time
    blocked on I/O, so the odd extra one has something to do. It is a
    reasonable first guess and a poor final answer: the binding constraint on
    this stack is usually PostgreSQL's max_connections, NOT CPU. See
    DEFAULT_WORKER_CAP above, `workers` below, and docs/serving.md.
    """
    return min((2 * detect_cpus()) + 1, DEFAULT_WORKER_CAP)


# --------------------------------------------------------------------------
# Where it listens
# --------------------------------------------------------------------------
# Fixed, and deliberately not configurable. The port INSIDE a container is a
# property of the image, published to whatever the host wants by Compose
# (APP_PROD_PORT) or by the platform. Making it a variable invites a deployment
# where the container listens somewhere the health check does not look.
#
# 0.0.0.0 is required: 127.0.0.1 would accept connections only from inside the
# container, so the published port would appear dead.
bind = "0.0.0.0:8000"

# --------------------------------------------------------------------------
# Workers
# --------------------------------------------------------------------------
# ⚠️  THE ARITHMETIC THAT CAUSES OUTAGES
#
#     total database connections ≈ workers × threads per worker
#
# Each worker is a SEPARATE PROCESS holding its own connections, and under ASGI
# sync views run in a threadpool where Django connections are thread-local — so
# each busy thread holds one too. Measured on this stack with CONN_MAX_AGE=60:
# 20 concurrent requests to a SINGLE uvicorn process held 20 connections,
# against PostgreSQL's default max_connections of 100.
#
# Multiply that by the worker count. Raising workers to handle load is the most
# common way a well-intentioned change takes a service down: past
# max_connections PostgreSQL refuses new connections outright, and the symptom
# looks nothing like "too many workers". At scale the answer is PgBouncer, not
# a larger max_connections — each connection costs memory server-side.
#
# config/settings/base.py holds the other half of this (CONN_MAX_AGE);
# docs/serving.md works the arithmetic through with numbers.
#
# WEB_CONCURRENCY is honoured as a fallback because it is the de-facto standard
# name that PaaS platforms set for you, and gunicorn itself reads it. An
# explicit GUNICORN_WORKERS wins over it, so a platform's guess can be
# overridden without unsetting anything. NEITHER is capped — the cap applies
# only to the value computed when nothing was set at all.
#
# Compose forwards only `env_file` and the keys named in `environment:`, so a
# GUNICORN_* exported in your shell does NOT reach the container. Put it in
# .env, which both services already read. Verified the hard way: an exported
# GUNICORN_WORKERS=3 produced 25 workers.
workers = _env_int("GUNICORN_WORKERS", _env_int("WEB_CONCURRENCY", default_workers()))

# The ASGI worker class, as its own package.
#
# NOT "uvicorn.workers.UvicornWorker". That module still exists and still
# works, and importing it emits a DeprecationWarning naming `uvicorn-worker` as
# its replacement; it is slated for removal. A template must not ship a
# deprecated import that every derived project inherits.
#
# Configurable because a derived project may have a reason to substitute one —
# UvicornH11Worker for a pure-Python HTTP parser, for instance. The default is
# the one with uvloop and httptools behind it (the [standard] extra).
worker_class = _env_str("GUNICORN_WORKER_CLASS", "uvicorn_worker.UvicornWorker")

# Load the application in each worker AFTER forking, not once before.
#
# Explicitly False rather than left to the default, because preloading looks
# like a free memory saving and is not. The application imports Django, which
# can open a database connection during startup; a connection created before
# the fork is INHERITED by every worker, and several processes reading and
# writing the same socket corrupt each other's results in ways that surface as
# impossible query errors. Preloading also defeats gunicorn's rolling worker
# restart, since workers no longer re-import the code.
preload_app = False

# --------------------------------------------------------------------------
# Timeouts
# --------------------------------------------------------------------------
# ⚠️  `timeout` IS NOT A REQUEST TIMEOUT. This is the single most misread
# gunicorn setting, and getting it backwards is how a service ends up with no
# request deadline at all while its operator believes it has one.
#
# What it actually is: how long the arbiter waits for a worker's HEARTBEAT
# before deciding the worker is wedged and killing it. Under an async worker
# the heartbeat is sent from an asyncio callback on a timer (`callback_notify`
# in uvicorn_worker), completely independently of whether any request is
# progressing. A request that takes ten minutes keeps beating the whole time
# and is never interrupted.
#
# What it DOES catch, and the reason to keep it: a worker whose EVENT LOOP IS
# BLOCKED — synchronous CPU-bound work inside an `async def`, the classic
# async-Django mistake. Nothing else in the stack notices that; this does.
#
# A real request deadline belongs at the proxy in front (nginx
# proxy_read_timeout, an ALB idle timeout) or in the application itself.
# docs/serving.md records both cases as measured, not asserted.
timeout = _env_int("GUNICORN_TIMEOUT", 30)

# How long a worker gets to finish in-flight requests after SIGTERM before it
# is killed.
#
# 25, NOT gunicorn's own default of 30, and the five seconds are the point.
# This must expire BEFORE the platform's kill grace period, or the platform
# SIGKILLs mid-drain and the graceful timeout never gets to do its job. Thirty
# seconds is precisely the common platform default — Compose's
# `stop_grace_period` for app-prod, and Kubernetes'
# terminationGracePeriodSeconds. Equal values race.
#
# Raise this only together with the platform's grace period, keeping the gap.
# M6-05 owns the shutdown sequence and verifies it end to end; this value only
# has to avoid setting up the race.
graceful_timeout = _env_int("GUNICORN_GRACEFUL_TIMEOUT", 25)

# --------------------------------------------------------------------------
# Proxies
# --------------------------------------------------------------------------
# Which upstream addresses may be trusted to have set X-Forwarded-*. Gunicorn's
# default (127.0.0.1) is the safe one and is kept.
#
# Behind a load balancer the application sees the balancer's address, not
# 127.0.0.1, so the forwarded headers are ignored and the client IP in the
# access log is the balancer's. Set this to the balancer's address then — and
# note "*" trusts anyone who can reach the process, which is the same class of
# mistake as DJANGO_TRUST_PROXY_SSL_HEADER on a directly reachable app: a
# client can then forge the header and choose its own apparent IP and scheme.
forwarded_allow_ips = _env_str("GUNICORN_FORWARDED_ALLOW_IPS", "127.0.0.1")

# --------------------------------------------------------------------------
# Logging
# --------------------------------------------------------------------------
# Both streams to stdout/stderr, never to a file. A container's filesystem is
# ephemeral and the platform collects its streams; a log file inside a
# container is written, rotated by nobody, and lost on replacement.
# PYTHONUNBUFFERED=1 in the Dockerfile is what keeps the lines flowing rather
# than sitting in a buffer until the process is killed.
accesslog = "-"
errorlog = "-"

# ⚠️  `access_log_format` IS DELIBERATELY ABSENT, and its absence is a finding
# rather than an oversight.
#
# Under an ASGI worker, gunicorn does not write the access line — uvicorn does.
# The worker class hands uvicorn gunicorn's HANDLERS and then lets uvicorn's
# own `uvicorn.access` logger format the record, so gunicorn's
# `access_log_format` is never consulted. Measured: with a format string
# specifying request duration and a correlation header, the line emitted was
# still uvicorn's plain
#
#     172.30.0.1:42090 - "GET /api/v1/ping HTTP/1.1" 200
#
# Setting it anyway would ship configuration that silently does nothing, which
# is worse than not having it — someone would eventually rely on a field that
# was never going to appear.
#
# `accesslog` above IS load-bearing, and that was measured too: pointing it at
# /dev/null silences the access line entirely. It is the switch; it just is not
# the formatter.
#
# TODO(M6-04): request duration and the correlation identifier therefore have
# to come from Django middleware, not from a server format string. M6-04 also
# routes `gunicorn.error` and `uvicorn.access` through Django's LOGGING so
# production access lines are JSON alongside application logs.
#
# This file deliberately sets NO logconfig_dict. Gunicorn configures its
# loggers at startup, before the application is imported; Django's dictConfig
# then runs when settings are first read and can claim them, because it does
# not disable existing loggers. Defining a competing config here would be the
# first thing M6-04 has to remove.
