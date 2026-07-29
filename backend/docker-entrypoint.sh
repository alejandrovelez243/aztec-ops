#!/bin/sh
# Aztec Ops backend entrypoint — one image, three roles.
#
# The image knows HOW each process starts; docker-compose.yml only says WHICH one to start
# (`command: ["worker"]`). That split is the point: a start-up sequence written into compose is a
# start-up sequence that only exists on a laptop, and the production deploy would have to
# reinvent it in a different file with different quoting.
#
# Roles:
#   api      migrate, seed (outside production), then serve ASGI. It is the ONLY migrator: worker
#            and beat gate on this container's healthcheck, so neither can reach a table that does
#            not exist yet. Nothing else in the stack applies migrations, so scaling `api` to two
#            replicas is the one change that would need a separate job again.
#   worker   the single Celery worker: drains the outbox, runs every handler, executes the ticks.
#   beat     the schedule. Enqueues, executes nothing.
#
# Anything else is executed verbatim, so `docker compose run api python manage.py showmigrations`
# keeps working.

set -eu

role="${1:-api}"

case "${role}" in
    api)
        python manage.py migrate --noinput

        # Seeding is an idempotent upsert (every fixture row carries an explicit pk), so this is
        # safe on every restart and is how a re-seed happens after editing a fixture. It is
        # skipped in production, where the demo portfolio is not wanted and the seed's admin
        # account would be a credential nobody asked for.
        if [ "${ENVIRONMENT:-local}" != "production" ]; then
            python manage.py seed
        fi

        set -- uvicorn config.asgi:application --host 0.0.0.0 --port 8000
        # --reload is a property of the dev image (which sets UVICORN_RELOAD), not of the compose
        # file. The runtime target never sets it.
        if [ "${UVICORN_RELOAD:-false}" = "true" ]; then
            set -- "$@" --reload
        fi
        exec "$@"
        ;;

    worker)
        # --concurrency 2 because the work is database-bound and each process holds its own
        # PostgreSQL connection; raise this and raise `max_connections` with it, or do not raise it.
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
