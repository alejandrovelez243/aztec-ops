# Aztec Ops — task runner.
#
# Every target runs from the repository ROOT and reaches into backend/ itself, because the root is
# where a newcomer is standing after `git clone`. You should never have to cd to run a command.
#
# Three rules this file keeps:
#
#   1. A target never hides a failing exit code. No `|| true`, no `-` prefix, no swallowed pipe.
#      Make aborts a recipe on the first line that exits non-zero and propagates that status.
#   2. Nothing waits on a sleep. Ordering comes from Compose healthchecks (`up --wait`).
#   3. It works on the make that is already installed. macOS ships GNU Make 3.81, which has
#      neither `.ONESHELL` nor `.SHELLFLAGS` — so every recipe line is a self-contained command,
#      and anything needing shell state keeps it on one line with `\` continuations. A recipe
#      written as a multi-line script would silently run line-by-line here and fail on the first
#      `if`. Do not add one.
#
# Start here:
#
#   make            # this help
#   make up         # build, start, migrate, wait for health
#   make seed       # fixtures + recomputed scores (idempotent)

SHELL := /bin/bash
.DEFAULT_GOAL := help

COMPOSE := docker compose
# One place to change if the API service is ever renamed; every exec below goes through it.
API := $(COMPOSE) exec -T api
BACKEND := backend

# A foreign DJANGO_SETTINGS_MODULE leaking in from the developer's shell makes ruff, mypy and
# pytest configure a different project and fail in ways that have nothing to do with this code.
# Every host-side target is prefixed with this.
HOST_ENV := env -u DJANGO_SETTINGS_MODULE

.PHONY: help urls up down build ps logs logs-api logs-worker migrate makemigrations seed \
        shell dbshell superuser test test-local lint format typecheck check outbox reset clean env

## help: list every target with what it does
help:
	@grep -E '^## ' $(MAKEFILE_LIST) | sed -e 's/^## //' | awk -F': ' '{printf "  \033[1m%-16s\033[0m %s\n", $$1, $$2}'
	@echo ""
	@echo "  From a clean clone:  cp .env.example .env && make up && make seed"

# --- environment ---------------------------------------------------------------------

## env: create .env from .env.example if it does not exist yet
env: .env

# Created once and never overwritten, so a customised .env survives every `make up`.
.env:
	@cp .env.example .env
	@echo ".env created from .env.example — review it before deploying anywhere."

# --- lifecycle -----------------------------------------------------------------------

## build: build the images without starting anything
build: .env
	$(COMPOSE) build

## up: build and start the whole stack, waiting on healthchecks (api migrates on start)
up: .env
	$(COMPOSE) up -d --build --wait
	@$(MAKE) --no-print-directory urls

## urls: print where the running stack is listening
urls:
	@# The port is asked of Compose rather than read from a default, so a customised API_PORT is
	@# reported correctly instead of confidently wrong.
	@port=$$($(COMPOSE) port api 8000 | sed 's/.*://'); \
	  echo ""; \
	  echo "  API      http://localhost:$$port"; \
	  echo "  Admin    http://localhost:$$port/admin/"; \
	  echo "  Outbox   http://localhost:$$port/admin/events/outboxevent/"; \
	  echo "  Frontend http://localhost:$${FRONTEND_PORT:-4321}"; \
	  echo ""; \
	  echo "  Next:    make seed"

## down: stop everything, keeping the volumes
down:
	$(COMPOSE) down

## ps: show service status and health
ps:
	$(COMPOSE) ps

## reset: DESTRUCTIVE — drop volumes, rebuild, start, seed
reset:
	$(COMPOSE) down -v
	$(MAKE) up
	$(MAKE) seed

## clean: remove containers, volumes and the built images
clean:
	$(COMPOSE) down -v --rmi local --remove-orphans

# --- logs ----------------------------------------------------------------------------

## logs: follow the logs of every service
logs:
	$(COMPOSE) logs -f

## logs-api: follow just the API
logs-api:
	$(COMPOSE) logs -f api

## logs-worker: follow the event path — one worker runs the drain, the handlers and the ticks
logs-worker:
	$(COMPOSE) logs -f worker beat

# --- database ------------------------------------------------------------------------

## migrate: apply migrations inside the api container
migrate:
	$(API) python manage.py migrate --noinput

## makemigrations: generate migrations (writes into the bind-mounted source tree)
makemigrations:
	$(COMPOSE) exec api python manage.py makemigrations

## seed: load the fixtures, sync the code sequences and recompute (idempotent)
seed:
	@# The only management command left, and the only one that should be: it is bootstrap, it runs
	@# inside the container, and it is an upsert. The fixture order is a domain fact (foreign-key
	@# dependency order) and it lives in apps/portfolio/.../seed.py, which also realigns the
	@# business-code sequences and rebuilds the ranking. A copy in this file would be a second
	@# source of truth that drifts the first time a fixture is added.
	@#
	@# There is no `make recompute`. Rebuilding scores by hand is an operator action, not a
	@# developer one: it is the "Recompute priority for selected projects" action in the Django
	@# admin, or POST /api/v1/recompute. A make target would be a third caller that only
	@# works from a checkout of the repository.
	$(API) python manage.py seed

## shell: Django shell inside the api container
shell:
	$(COMPOSE) exec api python manage.py shell

## dbshell: psql against the compose database
dbshell:
	$(COMPOSE) exec postgres sh -c 'psql -U "$$POSTGRES_USER" -d "$$POSTGRES_DB"'

## superuser: create a Django admin user (interactive)
superuser:
	$(COMPOSE) exec api python manage.py createsuperuser

# --- quality -------------------------------------------------------------------------

## test: run the suite inside the api container, against the compose database
test:
	$(COMPOSE) exec -T -e ENVIRONMENT=test api pytest -q

## test-local: run the suite on the host, against the PostgreSQL published by compose
test-local:
	cd $(BACKEND) && $(HOST_ENV) DATABASE_URL="postgres://aztec:aztec@localhost:$${POSTGRES_PORT:-55433}/aztec" ENVIRONMENT=test uv run pytest -q

## lint: ruff check and format check, on the host
lint:
	cd $(BACKEND) && $(HOST_ENV) uv run ruff check apps config
	cd $(BACKEND) && $(HOST_ENV) uv run ruff format --check apps config

## format: apply ruff's fixes and formatting
format:
	cd $(BACKEND) && $(HOST_ENV) uv run ruff check --fix apps config
	cd $(BACKEND) && $(HOST_ENV) uv run ruff format apps config

## typecheck: mypy strict over apps and config
typecheck:
	cd $(BACKEND) && $(HOST_ENV) uv run mypy apps config

## check: django system checks, lint and types — the full pre-push gate
check:
	cd $(BACKEND) && $(HOST_ENV) uv run python manage.py check
	$(MAKE) lint
	$(MAKE) typecheck

# --- event bus -----------------------------------------------------------------------

## outbox: how many events are pending, dispatched and dead-lettered
outbox:
	@# The outbox table replaced XPENDING and XINFO GROUPS, and it is the better instrument: it
	@# lives in PostgreSQL, it survives a Redis restart, and /admin/events/outboxevent/ filters by
	@# the same three states and offers "Re-queue selected dead-lettered events" — which is what
	@# replaced stream replay. A growing `pending` means the worker is not draining.
	$(COMPOSE) exec postgres sh -c 'psql -X -U "$$POSTGRES_USER" -d "$$POSTGRES_DB" -c "SELECT count(*) FILTER (WHERE published_at IS NULL AND dead_lettered_at IS NULL) AS pending, count(*) FILTER (WHERE published_at IS NOT NULL) AS dispatched, count(*) FILTER (WHERE dead_lettered_at IS NOT NULL) AS dead_lettered FROM events_outboxevent"'
