#!/bin/sh
# Aztec Ops frontend entrypoint — Astro dev server.
#
# It exists so the Dockerfile's CMD is a name rather than a paragraph of shell, and so the one
# piece of real start-up logic below lives somewhere a person can read.
#
# `rm -f .astro/dev.json` is load-bearing. Astro records the running dev server's pid there, and
# that file lives in the bind-mounted source tree, so it OUTLIVES the container that wrote it: a
# `docker compose stop` (or any SIGKILL) leaves it behind. Every container numbers its processes
# from 1, so the recorded pid names a live process in the *next* container, Astro believes a second
# server is already running, and exits — a crash loop under `restart: on-failure`.
#
# Astro's own suggestion, `astro dev --force`, is worse than the disease: it kills the recorded
# pid, which in the new container is Astro's own npm parent. Deleting the lock is the honest fix —
# one container runs exactly one dev server, so a lock at startup can only be a corpse.

set -eu

if [ ! -f package.json ]; then
    echo "frontend/ has no package.json. Run: npm create astro@latest frontend" >&2
    exit 1
fi

rm -f .astro/dev.json

exec npm run dev -- --host "${HOST:-0.0.0.0}" --port "${PORT:-4321}"
