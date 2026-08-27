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
	@if [ -z "$$(find . -name '*.py' -not -path './.venv/*' -not -path './.git/*' -print -quit)" ]; then \
		printf '  Nothing to type-check: no Python source yet (arrives with M3-01).\n'; \
	else \
		$(UV) run mypy .; \
	fi

test: ## Run the test suite
	@if [ ! -d tests ]; then \
		printf '  No tests yet: tests/ does not exist.\n'; \
		printf '  pytest is configured in pyproject.toml; tests arrive with the quality-gates phase.\n'; \
	else \
		$(UV) run pytest; \
	fi

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
        lint format typecheck test check \
        build up down logs ps shell \
        migrate makemigrations migrations-check django-shell superuser db-shell \
        backlog-check backlog-plan backlog-sync \
        clean
