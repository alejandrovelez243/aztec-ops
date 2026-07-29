# syntax=docker/dockerfile:1.7
#
# Aztec Ops backend image. One image, three processes: api, worker (celery), beat. They differ
# only by `command` in docker-compose.yml, so a dependency added here reaches all of them at once
# and none of them can drift.
#
# Multi-stage on purpose: uv and the build cache never reach the final layer, so the runtime image
# carries the resolved virtualenv and nothing that produced it.
#
# Targets:
#   runtime  production — locked runtime dependencies only, no dev group.
#   dev      local Compose — adds pytest/ruff/mypy so `make test` and `make lint` run in-container.
#
# The build never reads the source dataset: only pyproject.toml, uv.lock and backend/ are copied,
# and the .xlsx lives outside this build context and is git-ignored anyway.

# --- builder -------------------------------------------------------------------------
FROM python:3.12-slim AS builder

# uv arrives as a static binary from its own distroless image; nothing is installed with pip.
COPY --from=ghcr.io/astral-sh/uv:0.9 /uv /uvx /usr/local/bin/

# The virtualenv lives outside /app deliberately. Compose bind-mounts the host's ./backend over
# /app for hot reload, and a mount hides everything under the mount point — a venv at /app/.venv
# would vanish the moment the container started.
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/venv

WORKDIR /app

# Dependencies first, in their own layer: application code changes far more often than the
# lockfile, so this layer survives almost every rebuild.
COPY pyproject.toml uv.lock ./

# --frozen: fail rather than re-resolve. The image matches uv.lock exactly or it does not build.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev

# A second, additive sync produces the dev environment. Kept as its own stage so the runtime
# image can never inherit pytest, ruff or mypy by accident.
FROM builder AS builder-dev
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen

# --- base runtime --------------------------------------------------------------------
FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PATH="/opt/venv/bin:${PATH}" \
    DJANGO_SETTINGS_MODULE=config.settings

# psycopg[binary] ships its own libpq, so no build toolchain and no libpq-dev are needed here.
# curl is not installed either: the healthcheck uses the interpreter that is already present.

# Non-root from the first instruction that writes anything. UID 1000 matches the usual host user,
# so a bind-mounted source tree stays writable on Linux without a chown dance.
RUN groupadd --gid 1000 app \
    && useradd --uid 1000 --gid 1000 --create-home --shell /bin/bash app

WORKDIR /app

COPY --chown=app:app . /app

USER app

EXPOSE 8000

# --- runtime (production) ------------------------------------------------------------
FROM base AS runtime
COPY --from=builder --chown=app:app /opt/venv /opt/venv

# Collected at BUILD time, not at start: the manifest storage fingerprints every file and raises
# on a missing one, so a broken asset fails the build instead of shipping an admin with no CSS.
# Outside production WHITENOISE_USE_FINDERS serves straight from the finders, so the dev stage
# needs none of this. A placeholder key is enough — settings are only imported, never used to
# reach the database.
RUN DJANGO_SECRET_KEY=build-time-only-not-used-at-runtime \
    ENVIRONMENT=production \
    python manage.py collectstatic --noinput --clear

CMD ["uvicorn", "config.asgi:application", "--host", "0.0.0.0", "--port", "8000"]

# --- dev (local compose) -------------------------------------------------------------
FROM base AS dev
COPY --from=builder-dev --chown=app:app /opt/venv /opt/venv
CMD ["uvicorn", "config.asgi:application", "--host", "0.0.0.0", "--port", "8000", "--reload"]
