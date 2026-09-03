# ---------------------------------------------------------------------------
# django-forge — routine tasks
# ---------------------------------------------------------------------------
# Run `make` or `make help` to list every target.
#
# Targets tagged [M2] or [M3] are defined but not usable yet: they drive files
# that arrive with those milestones. Targets tagged [local] drive git-ignored
# tooling that is not part of a clone at all. Either way, running one tells you
# exactly what is missing, rather than leaking a raw tool error.
#
# Variables can be overridden without editing this file:
#     make up COMPOSE="podman compose"

.DEFAULT_GOAL := help

# Fail a recipe on the first failing command rather than continuing into a
# worse state. Without this, only the LAST command's exit status is checked.
SHELL := /bin/bash
.SHELLFLAGS := -eu -o pipefail -c

UV      ?= uv
# Match the container user to the developer running make. A bind mount keeps
# numeric ownership, so without these the dev container writes files onto the
# host owned by uid 1000. `export` puts them in the environment of every recipe,
# where docker-compose.yml picks them up as build args.
export APP_UID := $(shell id -u)
export APP_GID := $(shell id -g)

# Pinned: a scanner whose rules change under you gives inconsistent results.
GITLEAKS_IMAGE ?= zricethezav/gitleaks:v8.30.1

COMPOSE ?= docker compose
SERVICE ?= app
MANAGE  ?= python manage.py

COMPOSE_FILE ?= docker-compose.yml
MANAGE_FILE  ?= manage.py

# Where `make db-dump` writes. Git-ignored (.gitignore), and deliberately inside
# the project rather than /tmp: a dump taken before a risky migration should
# still be there tomorrow. See docs/backups.md — these are a local convenience,
# NOT a backup strategy.
BACKUP_DIR ?= backups

# The local planning documents, and the tool that projects them onto GitHub.
# Both are git-ignored and therefore absent from a fresh clone, which is why
# the targets below check for them the same way the [M2]/[M3] targets do.
BACKLOG      ?= python -m tools.backlog_sync
BACKLOG_DIR  ?= tools/backlog_sync
ISSUES_FILE  ?= issues.local.md

# ---------------------------------------------------------------------------
# require — stop with an actionable message when a prerequisite is missing
# ---------------------------------------------------------------------------
# $(1) the path that must exist    $(2) the issue that delivers it
#
# The body is one continued logical line on purpose: $(call ...) expands inside
# a recipe, and an expansion containing bare newlines would break Make's recipe
# parsing.
define require
if [ ! -e "$(1)" ]; then \
	printf '\n  make %s is not available yet.\n\n' "$@"; \
	printf '    missing:      %s\n' "$(1)"; \
	printf '    delivered by: %s\n' "$(2)"; \
	printf '\n  Run "make help" to see what works today.\n\n'; \
	exit 1; \
fi
endef

##@ Help

help: ## Show this help
	@awk 'BEGIN {FS = ":.*##"; printf "\nUsage:\n  make <target>\n"} \
		/^[a-zA-Z0-9_-]+:.*?##/ { printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2 } \
		/^##@/ { printf "\n\033[1m%s\033[0m\n", substr($$0, 5) }' $(MAKEFILE_LIST)
	@printf '\n'

##@ Setup

install: ## Install runtime + development dependencies
	$(UV) sync

install-prod: ## Install runtime dependencies only (what production images use)
	$(UV) sync --frozen --no-dev

lock: ## Re-resolve dependencies into uv.lock
	$(UV) lock

upgrade: ## Upgrade dependencies to newest allowed versions, then re-lock
	$(UV) lock --upgrade

##@ Quality

lint: ## Lint with ruff
	$(UV) run ruff check .

format: ## Format with ruff
	$(UV) run ruff format .

typecheck: ## Type-check with mypy
	@# The django-stubs plugin (pyproject.toml) imports Django settings to
	@# resolve model fields, and base.py requires DATABASE_URL with no
	@# fallback. A clean checkout has no .env, so a dummy is supplied rather
	@# than have type-checking fail on a correctly set-up machine. Nothing
	@# connects. `make audit` uses the same approach.
	@if [ -z "$$(find . -name '*.py' -not -path './.venv/*' -not -path './.git/*' -print -quit)" ]; then \
		printf '  Nothing to type-check: no Python source yet (arrives with M3-01).\n'; \
	else \
		DATABASE_URL="$${DATABASE_URL:-postgresql://mypy:mypy@localhost:5432/mypy}" \
		$(UV) run mypy .; \
	fi

test: ## Run the test suite
	@if [ ! -d tests ]; then \
		printf '  No tests yet: tests/ does not exist.\n'; \
		printf '  pytest is configured in pyproject.toml; tests arrive with the quality-gates phase.\n'; \
	else \
		$(UV) run pytest; \
	fi

test-db: ## [M4] Run the test suite in the container against real PostgreSQL
	@$(call require,$(COMPOSE_FILE),M2-04 (docker-compose.yml))
	@# The host suite runs on in-memory SQLite: fast, and it needs nothing
	@# running. This runs the SAME suite against the real engine, for the
	@# behaviour SQLite cannot answer for — constraints, transaction semantics,
	@# JSON fields, collation.
	@#
	@# It runs INSIDE the container because the database port is deliberately
	@# not published (M4-01), so the host cannot reach it.
	@#
	@# DJANGO_TEST_DATABASE_URL, not DATABASE_URL: the test layer only switches
	@# engines for this dedicated variable, so a developer's .env can never
	@# silently redirect the suite. Expanded in the container's shell, which is
	@# where DATABASE_URL is defined — hence the escaped $$.
	@#
	@# Deliberately NOT part of `make check` (tradeoff 62): that gate runs on
	@# the host and must not start failing whenever the stack is down.
	@# DJANGO_SETTINGS_MODULE is pinned here and must be. The container sets it
	@# to config.settings.development, and pytest-django prefers the ENVIRONMENT
	@# over pyproject.toml's ini value — so without this the suite silently runs
	@# under development settings, against the DEVELOPMENT database, and fails
	@# with a misleading "doesn't declare an explicit app_label".
	$(COMPOSE) exec -T $(SERVICE) sh -c 'DJANGO_SETTINGS_MODULE=config.settings.test \
		DJANGO_TEST_DATABASE_URL="$$DATABASE_URL" python -m pytest'

shutdown-demo: ## [M6] Demonstrate graceful shutdown: SIGTERM during a slow request
	@# The same test `make check` runs, with output shown, because M6-05's
	@# acceptance criterion is "verified by sending SIGTERM during a
	@# deliberately slow request" and that is worth being able to WATCH rather
	@# than take on trust from a green dot.
	@#
	@# A wrapper around the test rather than a second script doing the same
	@# thing by hand: two implementations of a demonstration drift, and the one
	@# in the Makefile is the one nobody runs in CI, so it drifts first.
	@#
	@# It starts a real uvicorn on a free port, holds a request open, signals
	@# the process mid-request, and asserts the response still arrives complete.
	@printf '\n  Starting a real server, holding a request open, and sending it SIGTERM.\n'
	@printf '  The request must come back 200 — see docs/ops.md for the sequence.\n\n'
	$(UV) run pytest tests/test_shutdown.py -v -k sigterm --log-cli-level=INFO

check: lint typecheck test ## Run lint, typecheck and test (local pre-push gate)

audit: ## Security audit — Django deploy checks + secret scan over full git history
	@printf '\n== Django deploy checks (production layer) ==\n'
	@# A clean environment with .env loading disabled: the audit must reflect the
	@# COMMITTED configuration, not whatever the developer happens to have locally.
	@env -i PATH="$$PATH" HOME=/tmp \
		DJANGO_SETTINGS_MODULE=config.settings.production \
		DJANGO_ENV_FILE=/nonexistent \
		DJANGO_DEBUG=0 \
		DJANGO_ALLOWED_HOSTS=example.com \
		DATABASE_URL=postgresql://audit:audit@localhost:5432/audit \
		DJANGO_SECRET_KEY="$$(.venv/bin/python -c 'import secrets;print(secrets.token_urlsafe(50))')" \
		.venv/bin/python manage.py check --deploy
	@printf '\n== Secret scan (gitleaks, FULL git history) ==\n'
	@# History, not the working tree: a credential deleted in a later commit is
	@# still present in earlier ones, and still needs rotating.
	@docker run --rm -v "$$PWD:/repo" -w /repo $(GITLEAKS_IMAGE) git /repo --no-banner
	@printf '\n'


##@ Docker — available from M2

# ALL_PROFILES enables every Compose profile at once. Targets that should act
# on the whole project regardless of which stack is up — building and tearing
# down — need it: without a profile flag, `docker compose down` walks past a
# running production-like container and then fails to remove the network,
# leaving the stack half up with no error that says so.
ALL_PROFILES := --profile '*'

build: ## [M2] Build the application image (both profiles)
	@$(call require,$(COMPOSE_FILE),M2-04 (docker-compose.yml))
	$(COMPOSE) $(ALL_PROFILES) build

up: ## [M2] Start the development stack
	@$(call require,$(COMPOSE_FILE),M2-04 (docker-compose.yml))
	$(COMPOSE) up -d

up-prod: ## [M2] Start the production-like stack
	@$(call require,$(COMPOSE_FILE),M2-04 (docker-compose.yml))
	@# The production settings layer requires these (M3-03) and refuses to
	@# start without them. Checked HERE rather than in docker-compose.yml,
	@# because Compose interpolates that file for every command — a guard
	@# there would break `make up`, `make migrate` and `make down` too.
	@missing=""; \
	for v in DJANGO_SECRET_KEY DJANGO_ALLOWED_HOSTS; do \
		val="$${!v-}"; \
		[ -z "$$val" ] && missing="$$missing $$v"; \
	done; \
	if [ -n "$$missing" ]; then \
		printf '\n  make up-prod needs:%s\n\n' "$$missing"; \
		printf '    The production settings layer requires them and will not start\n'; \
		printf '    without them. Put them in .env (see .env.example), or:\n\n'; \
		printf '      export DJANGO_SECRET_KEY=$$(python -c "from django.core.management.utils import get_random_secret_key as k; print(k())")\n'; \
		printf '      export DJANGO_ALLOWED_HOSTS=localhost,127.0.0.1\n\n'; \
		exit 1; \
	fi
	$(COMPOSE) --profile prod up -d app-prod

down: ## [M2] Stop every stack, keeping volumes and data
	@$(call require,$(COMPOSE_FILE),M2-04 (docker-compose.yml))
	$(COMPOSE) $(ALL_PROFILES) down

logs: ## [M2] Follow logs from all services
	@$(call require,$(COMPOSE_FILE),M2-04 (docker-compose.yml))
	$(COMPOSE) logs -f

ps: ## [M2] Show service status
	@$(call require,$(COMPOSE_FILE),M2-04 (docker-compose.yml))
	$(COMPOSE) ps

shell: ## [M2] Open a shell inside the application container
	@$(call require,$(COMPOSE_FILE),M2-04 (docker-compose.yml))
	$(COMPOSE) exec $(SERVICE) bash

db-shell: ## [M4] Open a psql session against the development database
	@$(call require,$(COMPOSE_FILE),M2-04 (docker-compose.yml))
	@# Runs inside the container, so no host port needs publishing and no local
	@# psql client is required.
	$(COMPOSE) exec db psql -U "$${POSTGRES_USER:-forge}" -d "$${POSTGRES_DB:-forge}"

db-dump: ## [M4] Dump the development database to backups/
	@$(call require,$(COMPOSE_FILE),M2-04 (docker-compose.yml))
	@# A LOCAL CONVENIENCE, NOT A BACKUP STRATEGY. Read docs/backups.md before
	@# anyone comes to depend on this.
	@#
	@# pg_dump runs INSIDE the db container, so client and server are always the
	@# same 17.6 build. A host-installed pg_dump of a different major version
	@# cannot read this server's data, and that skew is the classic way a
	@# "working" backup command starts failing after an unrelated upgrade.
	@#
	@# -T disables TTY allocation. The widely-repeated reason is that a TTY's
	@# line discipline rewrites LF to CRLF and corrupts the binary archive.
	@# TESTED HERE, and it did NOT reproduce: on Compose v5.5.0, dumps taken
	@# with -T, without it, and with -t forced under a pty were all 29,994
	@# bytes and all restored. Compose drops the pty when stdout is redirected.
	@#
	@# -T stays anyway, for a reason that survives the test: correctness must
	@# not depend on Compose's auto-detection of where stdout is pointing. The
	@# flag costs nothing and makes the requirement explicit — same as test-db.
	@#
	@# Written to <file>.partial and renamed only on success. The shell creates
	@# a redirect target BEFORE pg_dump runs, so a failed dump would otherwise
	@# leave a zero-byte file that looks exactly like a backup.
	@set -u; \
	db="$${POSTGRES_DB:-forge}"; \
	out="$${FILE:-$(BACKUP_DIR)/$$db-$$(date +%Y%m%d-%H%M%S).dump}"; \
	mkdir -p "$$(dirname "$$out")"; \
	trap 'rm -f "$$out.partial"' EXIT; \
	$(COMPOSE) exec -T db pg_dump \
		-U "$${POSTGRES_USER:-forge}" -d "$$db" --format=custom > "$$out.partial"; \
	mv "$$out.partial" "$$out"; \
	printf '\n  Dumped %s -> %s (%s)\n\n' "$$db" "$$out" "$$(du -h "$$out" | cut -f1)"

db-restore: ## [M4] Restore a dump, REPLACING the development database
	@$(call require,$(COMPOSE_FILE),M2-04 (docker-compose.yml))
	@# DESTRUCTIVE. Drops the development database and rebuilds it from FILE.
	@#
	@#     make db-restore FILE=backups/forge-20260828-141230.dump
	@#
	@# Drop-and-recreate rather than pg_restore --clean (tradeoff 69): --clean
	@# drops objects one at a time in archive order and fails noisily on
	@# anything the dump does not know about, leaving a half-restored database.
	@# An empty database has no such ordering to get wrong.
	@#
	@# The drop runs against the `postgres` maintenance database — you cannot
	@# drop the database you are connected to — and needs WITH (FORCE) because
	@# DJANGO_CONN_MAX_AGE keeps the app's connections open across requests.
	@# Without it: "database is being accessed by other users". FORCE is
	@# PostgreSQL 13+; this stack pins 17.6.
	@#
	@# The archive is listed with `pg_restore -l` BEFORE anything is dropped.
	@# Without that pre-flight the order is drop-then-discover: pointing this
	@# at a truncated or wrong file destroyed the database and only then
	@# reported "input file does not appear to be a valid archive". Measured
	@# during M4-06, on the first version of this target.
	@set -u; \
	if [ -z "$${FILE:-}" ]; then \
		printf '\n  make db-restore needs FILE=<dump>\n\n'; \
		printf '    e.g. make db-restore FILE=$(BACKUP_DIR)/forge-20260828-141230.dump\n'; \
		printf '    Run "make db-dump" first, or "ls $(BACKUP_DIR)" to see what you have.\n\n'; \
		exit 1; \
	fi; \
	if [ ! -f "$$FILE" ]; then \
		printf '\n  No such dump: %s\n\n' "$$FILE"; \
		exit 1; \
	fi; \
	db="$${POSTGRES_DB:-forge}"; user="$${POSTGRES_USER:-forge}"; \
	if ! $(COMPOSE) exec -T db pg_restore -l < "$$FILE" >/dev/null 2>&1; then \
		printf '\n  Not a readable dump: %s\n\n' "$$FILE"; \
		printf '    pg_restore cannot list its contents, so it will not restore\n'; \
		printf '    either. The database has NOT been touched.\n\n'; \
		exit 1; \
	fi; \
	if [ -z "$${FORCE:-}" ]; then \
		printf '\n  This DROPS the database "%s" and replaces it with\n' "$$db"; \
		printf '  the contents of %s.\n' "$$FILE"; \
		printf '  Anything not in that file is gone, with no undo.\n\n'; \
		printf '  Type the database name to continue: '; \
		read -r reply; \
		if [ "$$reply" != "$$db" ]; then \
			printf '\n  Aborted. Nothing was changed.\n\n'; \
			exit 1; \
		fi; \
		printf '\n'; \
	fi; \
	$(COMPOSE) exec -T db psql -v ON_ERROR_STOP=1 -q -U "$$user" -d postgres \
		-c "DROP DATABASE IF EXISTS \"$$db\" WITH (FORCE)" \
		-c "CREATE DATABASE \"$$db\" OWNER \"$$user\""; \
	$(COMPOSE) exec -T db pg_restore \
		--no-owner --no-privileges --exit-on-error \
		-U "$$user" -d "$$db" < "$$FILE"; \
	printf '  Restarting %s: its pooled connections were terminated by the drop.\n' "$(SERVICE)"; \
	$(COMPOSE) restart $(SERVICE) >/dev/null; \
	tables=$$($(COMPOSE) exec -T db psql -tAX -U "$$user" -d "$$db" \
		-c "SELECT count(*) FROM information_schema.tables WHERE table_schema='public'"); \
	printf '\n  Restored %s from %s — %s tables in public.\n\n' "$$db" "$$FILE" "$$tables"

##@ Django — available from M3

migrate: ## [M3] Apply database migrations
	@$(call require,$(MANAGE_FILE),M3-01 (manage.py))
	$(COMPOSE) exec $(SERVICE) $(MANAGE) migrate

makemigrations: ## [M3] Generate migrations from model changes
	@$(call require,$(MANAGE_FILE),M3-01 (manage.py))
	$(COMPOSE) exec $(SERVICE) $(MANAGE) makemigrations

migrations-check: ## [M4] Fail if a model change has no migration (pre-push)
	@$(call require,$(MANAGE_FILE),M3-01 (manage.py))
	@# Catches the classic "works locally, fails on deploy": a model edited
	@# without running makemigrations. --check sets a non-zero exit status when
	@# a migration is missing; --dry-run guarantees nothing is written, so this
	@# is safe to run anywhere.
	@#
	@# Deliberately NOT part of `make check`. That target runs on the host,
	@# this one needs the container, so folding it in would make the pre-push
	@# gate fail whenever the stack happens to be down.
	$(COMPOSE) exec $(SERVICE) $(MANAGE) makemigrations --check --dry-run

seed: ## [M4] Load development seed data (superuser + permission groups)
	@$(call require,$(MANAGE_FILE),M3-01 (manage.py))
	@# Development only. The command refuses unless settings.SEED_ENABLED is
	@# true, which only the development and test layers set — and NOT via an
	@# environment variable, because a .env is read by both Compose profiles.
	@# A --force flag would put the override back within reach, which is why
	@# there is not one.
	$(COMPOSE) exec $(SERVICE) $(MANAGE) seed

django-shell: ## [M3] Open the Django REPL
	@$(call require,$(MANAGE_FILE),M3-01 (manage.py))
	$(COMPOSE) exec $(SERVICE) $(MANAGE) shell

superuser: ## [M3] Create a Django superuser
	@$(call require,$(MANAGE_FILE),M3-01 (manage.py))
	$(COMPOSE) exec $(SERVICE) $(MANAGE) createsuperuser

##@ Backlog — local tooling, not part of a fresh clone

backlog-check: ## [local] Validate the planning documents (no token, no network)
	@$(call require,$(BACKLOG_DIR),tools/backlog_sync (a local, git-ignored tool))
	@$(call require,$(ISSUES_FILE),the local planning documents)
	$(BACKLOG) check

backlog-plan: ## [local] Show what a GitHub sync would change, writing nothing
	@$(call require,$(BACKLOG_DIR),tools/backlog_sync (a local, git-ignored tool))
	@$(call require,$(ISSUES_FILE),the local planning documents)
	$(BACKLOG) plan

backlog-sync: ## [local] Make GitHub match the documents (creates/updates issues)
	@$(call require,$(BACKLOG_DIR),tools/backlog_sync (a local, git-ignored tool))
	@$(call require,$(ISSUES_FILE),the local planning documents)
	$(BACKLOG) apply

##@ Housekeeping

clean: ## Remove the virtualenv and tool caches (never touches .env or data)
	rm -rf .venv
	rm -rf .pytest_cache .ruff_cache .mypy_cache
	find . -type d -name __pycache__ -not -path './.git/*' -exec rm -rf {} +
	@printf '  Removed .venv and tool caches. .env and Docker volumes untouched.\n'

# Every target is phony: none produces a file of its own name. Without this a
# directory named `build` would silently make `make build` a no-op.
.PHONY: help install install-prod lock upgrade audit \
        lint format typecheck test test-db shutdown-demo check \
        build up down logs ps shell \
        migrate makemigrations migrations-check seed django-shell superuser \
        db-shell db-dump db-restore \
        backlog-check backlog-plan backlog-sync \
        clean
