# syntax=docker/dockerfile:1
# ---------------------------------------------------------------------------
# django-forge — multi-stage image
# ---------------------------------------------------------------------------
# A multi-stage build uses one image to install dependencies (which can need
# compilers and header files), then copies only the finished result into a
# second, much smaller runtime image. The build tools never reach production,
# which cuts both image size and attack surface.
#
# Layer ordering is the other half of the value. Docker caches each instruction
# and reuses the cache until that instruction's inputs change. Dependency
# manifests are copied and installed BEFORE application source, so editing a
# Python file reuses the cached dependency layer instead of reinstalling
# everything. Reversing those two steps is the most common Dockerfile mistake.
#
# The third idea is privilege. A container runs as root unless told otherwise,
# so a single code-execution bug hands an attacker root inside the container and
# a much better shot at escaping it. The runtime stage below creates a dedicated
# unprivileged user, and deliberately does NOT give it write access to its own
# installed code. See the `USER` block near the bottom.

# ===========================================================================
# Stage 1 — builder: resolve and install dependencies
# ===========================================================================
# Pinned to an exact Python patch version and Debian release. Never use
# `latest`: it makes builds unreproducible and changes underneath you.
FROM python:3.12.8-slim-bookworm AS builder

# uv as a single static binary, pinned to the same version used locally.
# This is faster and easier to audit than `pip install uv`, and it pins uv in
# exactly one place. See docs/adr/0001-dependency-manager.md.
COPY --from=ghcr.io/astral-sh/uv:0.8.3 /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    # Copy packages into the venv rather than hardlinking them. The venv is
    # copied to another stage, so it has to be self-contained.
    UV_LINK_MODE=copy \
    # Use the interpreter already in this base image; do not fetch a second one.
    UV_PYTHON_DOWNLOADS=never \
    # Build the virtualenv OUTSIDE the project directory. In development the
    # host source tree is bind-mounted over /app (M2-07); a venv at
    # /app/.venv would be hidden by that mount and every import would fail
    # with a confusing "module not found". /opt/venv is never mounted over.
    UV_PROJECT_ENVIRONMENT=/opt/venv

WORKDIR /app

# --- dependency manifests ONLY -------------------------------------------
# This layer is invalidated only when pyproject.toml or uv.lock changes, so
# ordinary source edits do not trigger a reinstall.
COPY pyproject.toml uv.lock ./

# --frozen  install exactly what uv.lock pins; fail rather than re-resolve.
#           This is what makes the image reproducible.
# --no-dev  exclude ruff, mypy, pytest and friends. Test tooling in a
#           production image is wasted size and extra attack surface.
# The cache mount keeps uv's download cache across builds without storing it
# in a layer.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project

# --- application source comes AFTER dependencies -------------------------
# This ordering is the whole point of the two COPY steps: editing a Python file
# invalidates only this layer, so the dependency install above stays cached.
# Moving this above the install would reinstall every package on every edit.
#
# What actually lands here is governed by .dockerignore (M2-02) — .git, .venv
# and caches are excluded, which is what keeps this copy small.
COPY . .

# ===========================================================================
# Stage 1b — builder-dev: the same install, plus development tooling
# ===========================================================================
# Identical to `builder` except for the missing --no-dev, so ruff, mypy and
# pytest land in the venv. Kept as its own stage so `runtime` can never
# accidentally pick it up: production images must not ship test tooling.
#
# It builds on `builder`, so the expensive resolve above is reused rather than
# repeated — this adds the dev packages, it does not reinstall the runtime ones.
FROM builder AS builder-dev

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project

# ===========================================================================
# Stage 2 — runtime: only what is needed to serve a request
# ===========================================================================
# A fresh copy of the same base image. It inherits nothing from the builder,
# so uv, build tools, and caches are all left behind.
FROM python:3.12.8-slim-bookworm AS runtime

ENV \
    # Send logs straight out instead of buffering them, so nothing is lost
    # when a container is killed.
    PYTHONUNBUFFERED=1 \
    # Bytecode was already compiled in the builder.
    PYTHONDONTWRITEBYTECODE=1 \
    # Put the venv first on PATH so `python` is the venv's interpreter with no
    # activation step.
    PATH="/opt/venv/bin:$PATH" \
    # `USER` does NOT set HOME — without this line it stays /root, which the
    # app user cannot write, and anything caching under $HOME fails with a
    # confusing permission error rather than an obvious one.
    HOME=/home/app

WORKDIR /app

# The only thing carried over from the builder.
COPY --from=builder /opt/venv /opt/venv

# The application itself. Comes from the builder so both stages agree on
# exactly what was copied.
COPY --from=builder /app /app

# --- the unprivileged user ------------------------------------------------
# UID/GID are build arguments so a Compose dev profile can match the host user
# (M2-04). On Linux a bind mount keeps the host's numeric owner, so a container
# user with a different UID cannot write the files it just mounted. Defaults are
# the conventional 1000. ARG is per-stage: these have to be declared here, not
# once at the top of the file.
ARG APP_UID=1000
ARG APP_GID=1000

# `chown app:app /app` is NOT recursive, and that is the point: the working
# directory becomes writable, while the venv at /opt/venv stays root-owned and
# lives outside /app entirely — so a bind mount cannot expose it either. Merely
# readable. A process that gets code execution therefore cannot rewrite its own
# dependencies or pip-install into the venv.
RUN groupadd --gid ${APP_GID} app \
 && useradd --uid ${APP_UID} --gid ${APP_GID} --create-home --shell /bin/bash app \
 && chown app:app /app

# The entrypoint runs before the application on every start: it validates
# configuration and waits for the database, then `exec`s whatever command it
# was given. Copied and made executable while still root, because the app user
# must not be able to rewrite the script that runs as its own startup.
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod 0555 /usr/local/bin/docker-entrypoint.sh

# Set USER as late as possible: every step above needs to write as root, and
# the ones that follow do not. From here on the container has no privileges it
# does not need.
USER app

# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------
# Tells Docker whether the process inside is actually WORKING, not merely
# running. That distinction is what lets one service wait for another to be
# `healthy` rather than merely `started` — see the db block in
# docker-compose.yml, which M4-01 activates.
#
# What it proves (M6-01): the application is serving HTTP and its hard
# dependencies answer. It requests /readyz — READINESS, not liveness — because
# "healthy" here is the signal other services gate on with
# `condition: service_healthy`, and that question is "can it serve traffic",
# not "is the process alive".
#
# That choice is safe with Docker specifically, because Docker never restarts a
# container for being unhealthy: a database outage marks this container
# unhealthy and leaves it running, and it recovers on its own. Under an
# orchestrator the two questions get two probes — livenessProbe -> /healthz,
# readinessProbe -> /readyz. Pointing a livenessProbe at /readyz turns a
# database outage into a restart loop across every replica. See docs/ops.md.
#
# Python, not curl: the slim image has neither curl nor wget, and adding one to
# run a health check is a package and an attack surface for something the
# interpreter already does. `http.client` rather than `urllib.request` because
# it returns a non-2xx response instead of raising, so a 503 exits 1 cleanly
# rather than through a traceback.
#
# 8000 is hardcoded, and correct: it is the port the application binds INSIDE
# the container in both Compose profiles. APP_PORT and APP_PROD_PORT only map
# the host side.
#
# The values are chosen, not copied:
#   --interval=30s      this catches a container that has gone bad; it is not
#                       sub-second failover. Polling harder is constant load
#                       for no benefit, and it runs for the container's life.
#   --timeout=5s        the client gives up at 4s, so the probe reports a clean
#                       failure a second before Docker kills it — a killed
#                       probe tells you nothing about what went wrong.
#   --start-period=40s  failures in this window do NOT count toward retries.
#                       Raised from 10s with M6-01: that value was sized for
#                       `import django`, but docker-entrypoint.sh now waits up
#                       to DB_WAIT_TIMEOUT (30s) for the database BEFORE the
#                       server starts. Too short and a slow first boot is
#                       reported as failure.
#   --retries=3         three consecutive failures before `unhealthy`, so one
#                       transient blip does not flap the service.
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
    CMD ["python", "-c", "import http.client,sys; c=http.client.HTTPConnection('127.0.0.1',8000,timeout=4); c.request('GET','/readyz'); sys.exit(0 if c.getresponse().status==200 else 1)"]

# ENTRYPOINT is set in the image rather than in Compose, so every launch path
# — docker run, both Compose profiles, any future deployment — goes through the
# same startup sequence. Whatever is passed as CMD (or as a Compose `command:`)
# arrives as this script's "$@" and is exec'd.
ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]

# ---------------------------------------------------------------------------
# Stop signal (M6-05)
# ---------------------------------------------------------------------------
# SIGTERM is already Docker's default, so this line changes no behaviour. It is
# here because it is the ONE end of the shutdown contract that is otherwise
# invisible: everything else about the drain — `exec` in the entrypoint, exec
# form below, `graceful_timeout` in config/gunicorn.py, `stop_grace_period` in
# docker-compose.yml — is written down somewhere a reader can find it, and the
# signal that starts the whole sequence was the one thing left implicit.
#
# It also fails loudly rather than quietly if someone changes it. Gunicorn
# treats SIGTERM as "graceful stop" and SIGQUIT as "stop NOW, drop what you are
# holding"; a STOPSIGNAL of SIGQUIT would look like a tidy-up and would silently
# turn every deploy back into dropped requests. See docs/ops.md.
STOPSIGNAL SIGTERM

# ---------------------------------------------------------------------------
# The production server (M6-02)
# ---------------------------------------------------------------------------
# Gunicorn supervising Uvicorn workers. Gunicorn forks and restarts processes;
# Uvicorn speaks ASGI. Django's `runserver` is not here and never will be — it
# is single-threaded, unoptimised, WSGI-only, and explicitly not for production.
#
# Exec form, so this is exec'd directly with no intervening shell. The shell
# form would leave `sh` as the process the entrypoint execs, and SIGTERM would
# reach the shell rather than gunicorn — the failure M2-05's `exec` exists to
# prevent, reintroduced one line later. M6-05 depends on this too.
#
# ALL TUNING LIVES IN config/gunicorn.py, not in flags here. `python:` loads it
# as a module rather than a file path, so it resolves regardless of working
# directory. One file to read, one file to change, and the Compose
# production-like service overrides none of it — it runs the image as built.
#
# `-c` comes AFTER the application because gunicorn accepts options in either
# position; keeping the app first matches how the command is usually read
# aloud: run this application, with this configuration.
CMD ["gunicorn", "config.asgi:application", "-c", "python:config.gunicorn"]

# ===========================================================================
# Stage 3 — dev: runtime plus the tooling needed to test against PostgreSQL
# ===========================================================================
# What the development Compose service builds (`target: dev`). It is `runtime`
# with the development virtualenv swapped in, so the two profiles still share
# every other property — user, entrypoint, health check, layout — and cannot
# drift apart.
#
# WHY THIS EXISTS. The test suite has to be runnable against real PostgreSQL,
# and the database port is deliberately not published (M4-01), so the tests
# have to run *inside* the network. That needs pytest in the image, and pytest
# must never be in `runtime`.
#
# `runtime` is untouched by this stage. Verify that with:
#     docker compose run --rm app-prod python -m pytest    # must fail
FROM runtime AS dev

# Copied as root-owned and merely readable by `app`, exactly as the runtime
# venv is: the application user must not be able to rewrite its own
# dependencies. --chown is therefore deliberately absent.
COPY --from=builder-dev /opt/venv /opt/venv

USER app
