# syntax=docker/dockerfile:1.7
#
# Aztec Ops frontend image — Astro 7 dev server.
#
# Node 22 is not a preference: Astro 7 requires Node >= 22.12 and the developer's host runs Node
# 20. The container owns the runtime precisely so that mismatch stops being a problem anybody has
# to solve on their machine.
#
# One target, `dev`, because this stack is the local stack: `astro build` output is served by a
# static host in a deploy, not by this image. The start-up sequence lives in docker-entrypoint.sh
# rather than in a compose `command` — the compose file names the service, the image knows how it
# starts.

FROM node:22-alpine AS dev

ENV NODE_ENV=development \
    # Astro's dev server binds 127.0.0.1 by default, which is unreachable from outside the
    # container. HOST makes it bind every interface; PORT matches the published port.
    HOST=0.0.0.0 \
    PORT=4321

WORKDIR /app

# node:22-alpine already ships a non-root `node` user (uid 1000). Owning /app up front means the
# dev server can write .astro/ and node_modules/ without ever running as root.
RUN chown -R node:node /app
USER node

# Manifest and lockfile first, in their own layer: application code changes far more often than
# the dependency set, so this layer survives almost every rebuild. `npm ci` and nothing else — the
# image matches package-lock.json exactly or it does not build.
COPY --chown=node:node package.json package-lock.json ./
RUN npm ci

# Copied to /usr/local/bin, not left under /app: Compose bind-mounts the host's ./frontend over
# /app, and a mount hides everything beneath it.
COPY --chown=node:node docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

COPY --chown=node:node . /app

EXPOSE 4321

ENTRYPOINT ["docker-entrypoint.sh"]
