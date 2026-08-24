# ---------------------------------------------------------------------------
# django-forge — routine tasks
# ---------------------------------------------------------------------------
# Run `make` or `make help` to list every target.
#
# Targets tagged [M2] or [M3] are defined but not usable yet: they drive files
# that arrive with those milestones. Running one tells you exactly what is
# missing and which issue delivers it, rather than leaking a raw tool error.
#
# Variables can be overridden without editing this file:
#     make up COMPOSE="podman compose"

.DEFAULT_GOAL := help

# Fail a recipe on the first failing command rather than continuing into a
# worse state. Without this, only the LAST command's exit status is checked.
SHELL := /bin/bash
.SHELLFLAGS := -eu -o pipefail -c

UV      ?= uv
COMPOSE ?= docker compose
SERVICE ?= app
MANAGE  ?= python manage.py

COMPOSE_FILE ?= docker-compose.yml
MANAGE_FILE  ?= manage.py

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

##@ Django — available from M3

migrate: ## [M3] Apply database migrations
	@$(call require,$(MANAGE_FILE),M3-01 (manage.py))
	$(COMPOSE) exec $(SERVICE) $(MANAGE) migrate

makemigrations: ## [M3] Generate migrations from model changes
	@$(call require,$(MANAGE_FILE),M3-01 (manage.py))
	$(COMPOSE) exec $(SERVICE) $(MANAGE) makemigrations

django-shell: ## [M3] Open the Django REPL
	@$(call require,$(MANAGE_FILE),M3-01 (manage.py))
	$(COMPOSE) exec $(SERVICE) $(MANAGE) shell

superuser: ## [M3] Create a Django superuser
	@$(call require,$(MANAGE_FILE),M3-01 (manage.py))
	$(COMPOSE) exec $(SERVICE) $(MANAGE) createsuperuser

##@ Housekeeping

clean: ## Remove the virtualenv and tool caches (never touches .env or data)
	rm -rf .venv
	rm -rf .pytest_cache .ruff_cache .mypy_cache
	find . -type d -name __pycache__ -not -path './.git/*' -exec rm -rf {} +
	@printf '  Removed .venv and tool caches. .env and Docker volumes untouched.\n'

# Every target is phony: none produces a file of its own name. Without this a
# directory named `build` would silently make `make build` a no-op.
.PHONY: help install install-prod lock upgrade \
        lint format typecheck test check \
        build up down logs ps shell \
        migrate makemigrations django-shell superuser \
        clean
