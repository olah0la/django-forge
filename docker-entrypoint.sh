#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# django-forge — container entrypoint
# ---------------------------------------------------------------------------
# Runs after the container starts but before the application serves traffic:
#
#   1. validate required configuration
#   2. wait for the database to genuinely accept queries
#   3. hand off to the application process
#
# The point is to turn a class of confusing runtime failures into one clear
# message at startup. A missing setting should say which setting; a database
# that is not up yet should be waited for, not crashed into.
#
# -e  stop at the first failing command instead of continuing into a worse state
# -u  treat an unset variable as an error, so a typo fails loudly
# -o pipefail  a failure anywhere in a pipeline fails the pipeline
set -euo pipefail

log() { printf '  entrypoint: %s\n' "$*"; }
die() { printf '\n  entrypoint: %s\n\n' "$*" >&2; exit 1; }

# ---------------------------------------------------------------------------
# 1. Required environment variables
# ---------------------------------------------------------------------------
# Declared as DATA, not logic: M3-03 extends this list without touching the
# loop below. APP_REQUIRED_VARS lets a deployment add its own requirements
# (and lets this behaviour be tested before M3-03 exists).
#
# TODO(M3-03): add the settings the typed configuration layer requires, e.g.
#     DJANGO_SETTINGS_MODULE SECRET_KEY DATABASE_URL
REQUIRED_VARS="${APP_REQUIRED_VARS:-}"

missing=()
for var in ${REQUIRED_VARS}; do
    # Indirect expansion: ${!var} is the value of the variable *named* by $var.
    # :- guards against `set -u` aborting before the check can report anything.
    if [[ -z "${!var:-}" ]]; then
        missing+=("${var}")
    fi
done

# Report every missing variable at once. Reporting only the first means fixing
# one, restarting, and immediately hitting the next.
if (( ${#missing[@]} > 0 )); then
    printf '\n  entrypoint: missing required environment variable(s):\n\n' >&2
    for var in "${missing[@]}"; do printf '      %s\n' "${var}" >&2; done
    printf '\n  Set them in .env or in the service environment, then start again.\n\n' >&2
    exit 1
fi

# ---------------------------------------------------------------------------
# 2. Wait for the database
# ---------------------------------------------------------------------------
# There is no database service yet — M4-01 adds PostgreSQL to the Compose
# stack. Until DATABASE_URL is set there is nothing to wait for, so say so and
# carry on rather than blocking a container that has no database to reach.
DB_WAIT_TIMEOUT="${DB_WAIT_TIMEOUT:-30}"

if [[ -z "${DATABASE_URL:-}" ]]; then
    log "no DATABASE_URL set — skipping database wait (PostgreSQL arrives with M4-01)."
else
    log "waiting up to ${DB_WAIT_TIMEOUT}s for the database to accept connections..."

    # Why a real connection instead of a TCP port check: a PostgreSQL container
    # starts listening on 5432 noticeably before it will answer a query. A port
    # check therefore reports "ready" too early, and the application crashes on
    # its first statement. psycopg is already a runtime dependency, so this
    # costs nothing extra.
    # Measure WALL-CLOCK time, not iterations. Each attempt costs its own
    # connect_timeout plus the sleep, so counting loops would make the real
    # timeout roughly triple the configured value. $SECONDS is bash's built-in
    # elapsed-seconds counter.
    started_at=${SECONDS}
    until python - <<'PY' 2>/dev/null
import os, sys
import psycopg
try:
    with psycopg.connect(os.environ["DATABASE_URL"], connect_timeout=2) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT 1")
    sys.exit(0)
except Exception:
    sys.exit(1)
PY
    do
        if (( SECONDS - started_at >= DB_WAIT_TIMEOUT )); then
            # Show where it was actually trying to connect, with the password
            # stripped — this message ends up in logs.
            target=$(python - <<'PY' 2>/dev/null || echo "(unparseable DATABASE_URL)"
import os
from urllib.parse import urlsplit
u = urlsplit(os.environ["DATABASE_URL"])
print(f"{u.hostname}:{u.port or 5432}{u.path}")
PY
)
            die "database not reachable at ${target} after ${DB_WAIT_TIMEOUT}s. Is it running?"
        fi
        sleep 1
    done

    log "database is accepting connections (after $(( SECONDS - started_at ))s)."
fi

# ---------------------------------------------------------------------------
# 3. Migrations — deliberately NOT run here
# ---------------------------------------------------------------------------
# Applying migrations automatically on startup is tempting and is a common
# production incident:
#
#   * During a rolling deploy every replica starts at once and they race to
#     apply the same migration.
#   * A long migration blocks startup past the platform's health timeout, so
#     the container is killed mid-migration and restarted — repeatedly.
#
# They are run as a deliberate step instead: `make migrate` locally, and a
# release job or one-off task in a deployment. The full workflow, including the
# operations that cause outages, is in docs/migrations.md.
#
# If a derived project does decide to migrate here, two conditions are not
# optional: gate it behind an opt-in variable, and ensure only ONE replica can
# run it at a time (an advisory lock, or a separate init container/job).

# ---------------------------------------------------------------------------
# 4. Hand off to the application
# ---------------------------------------------------------------------------
# `exec` REPLACES this shell with the command, so the application becomes
# PID 1 and receives SIGTERM directly from Docker.
#
# Without exec, this script would stay PID 1 and absorb the signal: the
# platform would wait the full grace period and then SIGKILL the container,
# dropping every in-flight request on every deploy. That failure only shows up
# in production and is hard to attribute. M6-05 depends on this line.
exec "$@"
