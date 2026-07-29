# syntax=docker/dockerfile:1.7
#
# Aztec Ops frontend image — Astro 7 dev server. Node 22 because Astro 7 requires >= 22.12.
# The start-up sequence lives in docker-entrypoint.sh; compose only names the service.

FROM node:22-alpine AS dev

ENV NODE_ENV=development \
    # Astro binds 127.0.0.1 by default, which is unreachable from outside the container.
    HOST=0.0.0.0 \
    PORT=4321

WORKDIR /app

# node:22-alpine ships a non-root `node` user (uid 1000).
RUN chown -R node:node /app
USER node

# Dependencies in their own layer, so application edits do not reinstall them. `.npmrc` carries
# legacy-peer-deps and `npm ci` fails without it.
COPY --chown=node:node package.json package-lock.json .npmrc ./
RUN npm ci

COPY --chown=node:node . /app

EXPOSE 4321

# `rm -f .astro/dev.json` is load-bearing: Astro records the dev server's pid there, the file lives
# in the bind-mounted source tree, and it outlives the container that wrote it. Every container
# numbers its processes from 1, so the stale pid names a live process in the next one, Astro
# believes a second server is already running and exits — a crash loop under `restart: on-failure`.
CMD ["sh", "-c", "rm -f .astro/dev.json && npm run dev -- --host 0.0.0.0 --port 4321"]
