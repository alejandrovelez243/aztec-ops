#!/bin/sh
# Aztec Ops backend — one image, three roles. Compose says which role; this says how it starts.
#
#   api      migrate, seed (outside production), then serve ASGI. The only migrator: worker and
#            beat gate on its healthcheck, so neither can reach a table that does not exist yet.
#   worker   the single Celery worker — drains the outbox, runs every handler, executes the ticks.
#   beat     the schedule. Enqueues, executes nothing.
#
# Anything else runs verbatim, so `docker compose run api python manage.py showmigrations` works.

set -eu

case "${1:-api}" in
    api)
        python manage.py migrate --noinput

        # An idempotent upsert, so this is safe on every restart and is how a re-seed happens
        # after editing a fixture. Skipped in production: nobody asked for a demo portfolio there.
        if [ "${ENVIRONMENT:-local}" != "production" ]; then
            python manage.py seed
        fi

        set -- uvicorn config.asgi:application --host 0.0.0.0 --port 8000
        if [ "${UVICORN_RELOAD:-false}" = "true" ]; then
            set -- "$@" --reload
        fi
        exec "$@"
        ;;

    worker)
        # Database-bound work, one PostgreSQL connection per process: raise the concurrency and
        # raise `max_connections` with it, or do not raise it.
        exec celery -A config worker \
            --loglevel "${CELERY_LOG_LEVEL:-info}" \
            --concurrency "${CELERY_CONCURRENCY:-2}"
        ;;

    beat)
        exec celery -A config beat --loglevel "${CELERY_LOG_LEVEL:-info}"
        ;;

    *)
        exec "$@"
        ;;
esac
