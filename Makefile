# Aztec Ops — task runner.
#
# Every target runs from the repository ROOT and reaches into backend/ itself, because the root is
# where a newcomer is standing after `git clone`. You should never have to cd to run a command.
#
# Four rules this file keeps:
#
#   1. A target never hides a failing exit code. No `|| true`, no `-` prefix, no swallowed pipe.
#   2. Nothing waits on a sleep. Ordering comes from Compose healthchecks (`up --wait`).
#   3. It works on the make that is already installed. macOS ships GNU Make 3.81, which has
#      neither `.ONESHELL` nor `.SHELLFLAGS` — so every recipe line is a self-contained command,
#      and anything needing shell state keeps it on one line with `\` continuations. A recipe
#      written as a multi-line script would silently run line-by-line here and fail on the first
#      `if`. Do not add one.
#   4. A target exists only when it is the shortest correct way to do something. Anything the
#      stack already does on its own does not get a button here — a wrapper that duplicates
#      behaviour is a second source of truth waiting to drift. That is why there is no `outbox`
#      target: /admin/events/outboxevent/ already filters pending, dispatched and dead-lettered,
#      searches by entity and correlation id, and offers the re-queue action. Three counts in a
#      terminal are strictly less than that.
#
# Start here:
#
#   cp .env.example .env   # then fill in SEED_USER_PASSWORD and the DJANGO_SUPERUSER_* values
#   make up                # build, start, migrate, seed, wait for health
#
# There is deliberately no `make seed`, no `make migrate` and no `make superuser`. `up` runs
# migrations and then `seed` inside the api container before the port is bound, and `seed` creates
# the admin account from the environment. Seeding is an upsert, so `make up` again is how you
# re-seed after editing a fixture or filling in the credentials.
#
# There is also no `make recompute`. Rebuilding scores by hand is an operator action, not a
# developer one: the "Recompute priority for selected projects" action in the Django admin, or
# POST /api/v1/recompute. A make target would be a third caller that only works from a checkout.

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

.PHONY: help up down build ps logs reset clean makemigrations shell dbshell test lint format

## help: list every target with what it does
help:
	@grep -E '^## ' $(MAKEFILE_LIST) | sed -e 's/^## //' | awk -F': ' '{printf "  \033[1m%-16s\033[0m %s\n", $$1, $$2}'
	@echo ""
	@echo "  From a clean clone:  cp .env.example .env  →  fill in the credentials  →  make up"

# Created once and never overwritten, so a customised .env survives every `make up`.
.env:
	@cp .env.example .env
	@echo ".env created from .env.example — fill in SEED_USER_PASSWORD and DJANGO_SUPERUSER_* before continuing."

# --- lifecycle -----------------------------------------------------------------------

## up: build and start everything — migrates, seeds and waits on healthchecks
up: .env
	$(COMPOSE) up -d --build --wait
	@# The port is asked of Compose rather than read from a default, so a customised API_PORT is
	@# reported correctly instead of confidently wrong.
	@port=$$($(COMPOSE) port api 8000 | sed 's/.*://'); \
	  echo ""; \
	  echo "  API      http://localhost:$$port"; \
	  echo "  Admin    http://localhost:$$port/admin/"; \
	  echo "  Outbox   http://localhost:$$port/admin/events/outboxevent/"; \
	  echo "  Frontend http://localhost:$${FRONTEND_PORT:-4321}"; \
	  echo ""

## down: stop everything, keeping the volumes
down:
	$(COMPOSE) down

## build: build the images without starting anything
build: .env
	$(COMPOSE) build

## ps: show service status and health
ps:
	$(COMPOSE) ps

## logs: follow logs — every service, or one with `make logs s=worker`
logs:
	$(COMPOSE) logs -f $(s)

## reset: DESTRUCTIVE — drop the volumes and bring the stack back up from empty
reset:
	$(COMPOSE) down -v
	$(MAKE) up

## clean: remove containers, volumes and the built images
clean:
	$(COMPOSE) down -v --rmi local --remove-orphans

# --- development ---------------------------------------------------------------------

## makemigrations: generate migrations (writes into the bind-mounted source tree)
makemigrations:
	$(COMPOSE) exec api python manage.py makemigrations

## shell: Django shell inside the api container
shell:
	$(COMPOSE) exec api python manage.py shell

## dbshell: psql against the compose database
dbshell:
	$(COMPOSE) exec postgres sh -c 'psql -U "$$POSTGRES_USER" -d "$$POSTGRES_DB"'

# --- quality -------------------------------------------------------------------------

## test: run the suite inside the api container, against the compose database
test:
	$(COMPOSE) exec -T -e ENVIRONMENT=test api pytest -q

## lint: the full gate — django checks, ruff, and mypy strict
lint:
	cd $(BACKEND) && $(HOST_ENV) uv run python manage.py check
	cd $(BACKEND) && $(HOST_ENV) uv run ruff check apps config
	cd $(BACKEND) && $(HOST_ENV) uv run ruff format --check apps config
	cd $(BACKEND) && $(HOST_ENV) uv run mypy apps config

## format: apply ruff's fixes and formatting
format:
	cd $(BACKEND) && $(HOST_ENV) uv run ruff check --fix apps config
	cd $(BACKEND) && $(HOST_ENV) uv run ruff format apps config
