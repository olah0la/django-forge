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
# There is no application yet; it arrives with M3-01. When it does, add:
#     COPY . .
#     RUN uv sync --frozen --no-dev
# Keep those lines below the dependency install, or every source edit will
# reinstall every package.

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
# What it proves TODAY: the interpreter and the installed virtualenv are
# intact. There is no HTTP server yet; M6-01 replaces this with a request to
# the readiness endpoint. Note the slim image has no curl or wget, so that
# probe will be Python too — this is the shape it keeps.
#
# The values are chosen, not copied:
#   --interval=30s      this catches a container that has gone bad; it is not
#                       sub-second failover. Polling harder is constant load
#                       for no benefit, and it runs for the container's life.
#   --timeout=5s        ~60x the measured probe cost (85ms), so a loaded
#                       machine does not produce false failures.
#   --start-period=10s  failures in this window do NOT count toward retries.
#                       Too short and a slow first boot is reported as failure
#                       and the container is restarted in a loop.
#   --retries=3         three consecutive failures before `unhealthy`, so one
#                       transient blip does not flap the service.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import django"]

# ENTRYPOINT is set in the image rather than in Compose, so every launch path
# — docker run, both Compose profiles, any future deployment — goes through the
# same startup sequence. Whatever is passed as CMD (or as a Compose `command:`)
# arrives as this script's "$@" and is exec'd.
ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]

# There is no application to run yet. Fail with a message that says what is
# missing and which issue delivers it, rather than an opaque traceback.
# M6-02 replaces this with the real gunicorn invocation.
CMD ["sh", "-c", "echo; \
echo '  django-forge: no application in this image yet.'; \
echo; \
echo '    The Django project arrives with M3-01, and the production'; \
echo '    server command with M6-02.'; \
echo; \
echo '    The Python environment IS installed — inspect it with:'; \
echo '      docker run --rm -it <image> python'; \
echo; \
exit 1"]
