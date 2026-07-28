# syntax=docker/dockerfile:1.7
#
# Aztec Ops frontend image — Astro 7 dev server.
#
# Node 22 is not a preference: Astro 7 requires Node >= 22.12 and the developer's host runs Node
# 20. The container owns the runtime precisely so that mismatch stops being a problem anybody has
# to solve on their machine.
#
# The Astro source does not exist yet. This file is written so it *builds* today anyway: the
# dependency install is guarded, so an empty frontend/ produces a valid image instead of a broken
# build, and the failure — if you start it before scaffolding — happens at run time with a message
# that says exactly what to do. The compose `frontend` service stays commented out until
# `npm create astro@latest` has run; see the note in docker-compose.yml.

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

COPY --chown=node:node . /app

# npm ci when there is a lockfile, npm install when there is only a manifest, nothing when the
# project has not been scaffolded. `set -e` is implicit in the shell form here: any npm failure
# still fails the build — the guard skips work that is impossible, it never swallows an error.
RUN if [ -f package-lock.json ]; then \
        npm ci; \
    elif [ -f package.json ]; then \
        npm install; \
    else \
        echo "No package.json in frontend/ — image built without dependencies."; \
    fi

EXPOSE 4321

# Fails loudly and legibly if the project was never scaffolded, rather than exiting with a bare
# npm error nobody can act on.
CMD ["sh", "-c", "test -f package.json || { echo 'frontend/ has no package.json. Run: npm create astro@latest frontend'; exit 1; }; npm run dev -- --host 0.0.0.0 --port 4321"]
