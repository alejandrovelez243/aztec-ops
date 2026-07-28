# Contributing — Aztec Ops

Working agreement for anyone, human or agent, picking this project up cold.
Normative spec is `docs/ARCHITECTURE.md`; project rules are `CLAUDE.md`; product truth is
`PRODUCT.md`. This file is about how work gets done, not what the system does.

## Contents

1. [Prerequisites and versions](#1-prerequisites-and-versions)
2. [First-time setup from a clean clone](#2-first-time-setup-from-a-clean-clone)
3. [Dependency policy — CLI only](#3-dependency-policy--cli-only)
4. [Pre-commit](#4-pre-commit)
5. [Git Flow](#5-git-flow)
6. [Migrations](#6-migrations)
7. [How to add a use case](#7-how-to-add-a-use-case)
8. [How to add a workflow state](#8-how-to-add-a-workflow-state)
9. [How to add a prioritization signal](#9-how-to-add-a-prioritization-signal)
10. [How to add an event](#10-how-to-add-an-event)
11. [Who to hand a task to](#11-who-to-hand-a-task-to)
12. [Definition of done](#12-definition-of-done)

### The rest of the documentation

| File | What it answers |
|---|---|
| [`README.md`](../README.md) | What the system is, how to run it, and the prioritization criterion |
| [`docs/ARCHITECTURE.md`](ARCHITECTURE.md) | The normative spec: decisions, domain model, layers, event flow |
| [`docs/DATA_MODEL.md`](DATA_MODEL.md) | Every table, field, constraint and index |
| [`docs/API.md`](API.md) | The HTTP contract the frontend consumes, including SSE |
| [`docs/EVENTS.md`](EVENTS.md) | The canonical event catalog — register new topics here |
| [`docs/RUNBOOK.md`](RUNBOOK.md) | Operating it, and what to do when it breaks |
| [`docs/adr/`](adr/) | Why each decision was made, and what it cost |
| [`CLAUDE.md`](../CLAUDE.md) | The hard rules, loaded by AI tooling every session |
| [`PRODUCT.md`](../PRODUCT.md) | Product truth: users, purpose, constraints |
| [`DESIGN.md`](../DESIGN.md) | The visual system |

## 1. Prerequisites and versions

| Tool | Version | Needed for |
|---|---|---|
| Docker + Docker Compose | current | Everything. This is the supported path. |
| uv | 0.11+ | Managing Python dependencies and running `pre-commit` on the host. |
| Python | 3.12 | Only on the host, and only so `uv` can build the pre-commit environment. |
| Node | >= 22.12.0 | Only if you run the Astro dev server outside Docker. Astro 7 refuses to start below that. |
| Git | current | — |

Runtime versions (Django 6, django-ninja, PostgreSQL 16, Redis 7, Astro 7) are pinned by
`pyproject.toml`, `uv.lock` and `frontend/package.json`. Do not restate them in prose docs; they go
stale and then contradict the lockfile.

The container images own the runtime. A Node version below 22.12 on your machine does not block
you: the `web` service builds and runs Astro inside its own image, so `make up` works regardless
of the host toolchain. Install Node locally only if you want `npm run dev` outside Compose, and
in that case use a version manager rather than fighting the system Node.

Ports used: 8000 (`api`), 4321 (`web`), 5432 (`postgres`), 6379 (`redis`).

## 2. First-time setup from a clean clone

Install the git hooks **first**, before your first commit. A hook installed later does not
retroactively check what you already committed.

```bash
git clone git@github.com:alejandrovelez243/aztec-ops.git
cd aztec-ops

uv sync --project backend                # builds backend/.venv from backend/uv.lock
uv run --project backend pre-commit install
```

Then bring the system up:

```bash
cp .env.example .env

make up                                             # postgres, redis, api, relay, worker, ticker, frontend
docker compose exec api python manage.py migrate
make seed                                           # loaddata fixtures + recompute scores
docker compose exec api python manage.py createsuperuser
```

Then verify:

```bash
docker compose ps
curl -s localhost:8000/api/health
```

- API docs: http://localhost:8000/api/docs
- Django admin (taxonomies, workflows, outbox, DLQ): http://localhost:8000/admin/
- Frontend: http://localhost:4321

### Repository layout

```
backend/     Django project — apps/, config/, scripts/, pyproject.toml, uv.lock
frontend/    Astro project — src/pages, src/components, src/lib
docs/        Architecture, data model, API, events, runbook, ADRs
data/raw/    Normalized source dataset (gitignored; fixtures are the committed form)
```

`make seed` is idempotent — fixtures carry stable primary keys, so `loaddata` upserts. Run it
twice and the database is identical. If it is not, that is a bug for `seed-data-engineer`.

Operating the system day to day (relay in the foreground, stream inspection, DLQ, resets) is in
the `aztec-local-dev` skill, not here.

## 3. Dependency policy — CLI only

Dependencies are added exclusively through the package manager's own CLI. Hand-editing
`pyproject.toml`, `package.json` or any lockfile to add a dependency is forbidden.

```bash
uv add --project backend <pkg>                      # Python runtime dependency
uv add --project backend --dev <pkg>                # Python dev dependency
uv remove <pkg>

npm create astro@latest           # scaffold the frontend (once)
npx astro add <integration>       # Astro integrations, through Astro's own command
npm install <pkg>                 # frontend runtime dependency
```

Why this is a rule and not a preference: the CLI resolves against the live registry, picks the
current compatible version, and writes the lockfile in the same step. A hand-written version
string does none of that — it encodes whatever version the writer remembered, skips the
resolver, and leaves the lockfile inconsistent with the manifest. The `uv lock --check`
pre-commit hook then fails, or worse, the lockfile is regenerated later and silently changes
versions nobody chose. `npx astro add` additionally writes the integration into `astro.config`,
which `npm install` alone does not.

Before using an API from any of these libraries, check its current documentation with
`context7`. Framework majors move; an assumed signature is how a build quietly targets a version
that no longer exists.

## 4. Pre-commit

Installed with `uv run --project backend pre-commit install`, before your first commit. Hooks that run on every commit:

| Hook | What it does |
|---|---|
| `ruff check --fix` | Lints staged Python and applies safe fixes. A remaining error fails the commit. |
| `ruff format` | Formats staged Python. |
| `uv lock --check` | Fails if `uv.lock` does not match `pyproject.toml` — catches a hand-edited manifest. |

If a hook rewrites a file, the commit aborts with the file modified. Re-stage and commit again.
Never pass `--no-verify`. If a hook is wrong, fix the hook configuration in a commit of its own.

Run the full pass over the repository without committing:

```bash
uv run --project backend pre-commit run --all-files
```

`make lint` (ruff + mypy strict over `domain/` and `services/`) is the wider gate and runs in the
container. Pre-commit is the fast subset, not a replacement for it.

## 5. Git Flow

Standard Git Flow. Two permanent branches, three kinds of temporary ones.

| Branch | Lives | Branches from | Merges into | Rule |
|---|---|---|---|---|
| `main` | forever | — | — | Only ever receives merges from `release/*` and `hotfix/*`. Every commit on it is tagged and deployable. Never commit directly. |
| `develop` | forever | `main` | `release/*` | Integration branch and the default target for pull requests. Never commit directly. |
| `feature/<slug>` | short | `develop` | `develop` | One unit of work. Delete after merge. |
| `release/<x.y.z>` | short | `develop` | `main` **and** `develop` | Only version bumps, docs and bug fixes. No new features. |
| `hotfix/<x.y.z>` | short | `main` | `main` **and** `develop` | Production is broken and cannot wait for a release. |

```
main      ──●────────────────────●───────────────●──   tags: v0.1.0, v0.1.1
             \                  /               /
release       \        ────────●               /       release/0.1.0
               \      /         \             /
develop   ──────●────●───────────●──────●────●─────
                 \  /                   /
feature           ●●                   ●               feature/priority-override
```

The branch prefix carries the same meaning as the commit type, so a branch holding a fix is
`feature/fix-relay-ack` only when it targets `develop`; a genuine production fix is
`hotfix/0.1.1`.

```bash
# start work
git switch develop && git pull
git switch -c feature/priority-override

# finish it — no fast-forward, so the feature stays visible as a unit in history
git switch develop && git pull
git merge --no-ff feature/priority-override
git push && git branch -d feature/priority-override

# cut a release
git switch -c release/0.1.0 develop
# ... version bump, changelog, fixes only ...
git switch main && git merge --no-ff release/0.1.0
git tag -a v0.1.0 -m "0.1.0"
git switch develop && git merge --no-ff release/0.1.0
git push --all && git push --tags
```

Rules that are not negotiable:

- `main` and `develop` are never committed to directly. Everything arrives by merge.
- Merges of a `feature`, `release` or `hotfix` branch use `--no-ff`, so the branch remains a
  readable unit in the history rather than dissolving into a line of commits.
- A `release` or `hotfix` branch is merged into **both** `main` and `develop`. Forgetting the
  second merge is how a fix silently disappears in the next release.
- Rebase your own feature branch onto `develop` freely before it is merged. Never rebase a branch
  someone else has pulled.
- Versions are `MAJOR.MINOR.PATCH` and tags are prefixed `v`.

### Commits

Conventional Commits, imperative mood, no trailing period:

```
feat(prioritization): add owner_saturation signal
fix(bus): ack duplicate events instead of leaving them pending
docs(architecture): document task.reassigned topic
chore(deps): add pytest-asyncio
```

Types in use: `feat`, `fix`, `refactor`, `docs`, `test`, `chore`, `perf`. Scope is the app or
area (`portfolio`, `workflow`, `prioritization`, `bus`, `api`, `web`, `seed`, `infra`).

One logical change per commit. A migration, the model change that caused it, and the fixture
update that follows it belong together; unrelated formatting does not.

## 6. Migrations

- One migration per logical change, named descriptively:
  `uv run python manage.py makemigrations <app> -n <descriptive_name>`.
- Read the generated file before committing it. Auto-generated does not mean correct — check the
  field order, the `related_name`s and any `RunPython`.
- Never edit an applied migration. Add a new one.
- Enforce invariants in the database where possible: `Meta.constraints` with `UniqueConstraint`
  and `CheckConstraint`, plus the indexes the command center's queries need.
- New taxonomy values, workflow states and transitions are **data**, not schema. They go into
  fixtures under `backend/apps/*/fixtures/`, not into a migration full of `TextChoices` and not into a
  Python constant.
- A data migration is acceptable for one thing: backfilling a column that already exists, or
  introducing a new `PriorityPolicy` version. It is not a substitute for a fixture.
- Migrations run inside the container: `docker compose exec api python manage.py migrate`.

## 7. How to add a use case

This is the path most changes take. Walk the layers in this order; each step depends only on the
ones above it. Owning skill: `.agents/skills/django-clean-arch/`. Worked example there:
"resolve a blocker on a project".

1. **`backend/apps/<context>/domain/errors.py`** — the typed error the use case can raise
   (`BlockerAlreadyResolved(DomainError)`). Pure Python, no Django import. It is mapped to HTTP
   later by the central handler, never caught in a view.
2. **`backend/apps/<context>/domain/events.py`** — the topic constant, and the typed payload model (a
   `pydantic.BaseModel`) if the topic is new. Reuse the spellings listed in `ARCHITECTURE.md` §6; do not invent a variant.
3. **`backend/apps/<context>/models.py` + migration** — only if the use case needs a field that does not
   exist. Fields, constraints, indexes. No business rules on the model.
4. **`backend/apps/<context>/repositories.py`** — the query the service needs, with its
   `select_related` / `select_for_update`. Skip this step for a plain primary-key lookup with no
   joins; add it the moment the same filter appears twice or a business rule ("what counts as an
   open blocker") is encoded in a `filter()`.
5. **`backend/apps/<context>/services/<use_case>.py`** — one module, one public function, decorated
   `@transaction.atomic`. In this order inside the transaction: mutate the aggregate, write the
   `ActivityRecord`, write the `OutboxEvent`. No Redis import anywhere under `services/`; the
   relay is the only process that publishes. `correlation_id` is threaded through from the API
   boundary.
6. **The outbox event** — written by the service with `enqueue_event(...)` in the same
   transaction. If the topic is new, follow §10 below before continuing.
7. **`backend/apps/<context>/api/schemas.py`** — typed input and output schemas. No bare `dict`
   responses, no importing `models` for a type hint.
8. **`backend/apps/<context>/api/routers.py`** — one route, one service call, return the schema. No `if`,
   no queryset, no per-view `try/except`.
9. **`backend/apps/<context>/admin.py`** — expose the new field only if the operation must edit it.
10. **Consumers** — the `ProjectSnapshot` rebuild and the risk evaluator already subscribe to the
    `project.*` and `blocker.*` families. Edit a consumer only if the topic is genuinely new.
11. **Tests** — a database-free test for the domain error condition; an integration test proving
    exactly one `ActivityRecord` and one `OutboxEvent` are written and that a rollback leaves
    zero of both; an API test for the error status code.
12. **Docs** — if the change altered the model, the topic list or an invariant, update
    `docs/ARCHITECTURE.md` in the same commit.

State changes are the one exception to step 5: no service assigns `workflow_state` directly.
They go through the transition service, which validates against `WorkflowTransition`.

## 8. How to add a workflow state

A new state is data. It must not require a deploy. Owning skill: `.agents/skills/aztec-domain/`
(state categories and the operational definitions), with layering from
`.agents/skills/django-clean-arch/`.

1. Add the `WorkflowState` row: `code`, `label` (Spanish, it is user-facing), `category`
   (`BACKLOG | IN_PROGRESS | BLOCKED | DONE | CANCELLED`), `order`, `color`, `is_initial` /
   `is_terminal`. The `category` is what the rest of the system queries — get it right, because
   no logic will ever branch on the new `code`.
2. Add the `WorkflowTransition` rows into and out of it. A state with no legal transition is
   unreachable. Set `requires_reason` and `requires_fields` (for example `next_step`) where the
   operation demands a justification.
3. Check `WorkflowBinding`: if the state belongs to only one engagement type's lifecycle, it
   lives in that workflow, not in the default one.
4. Mirror the rows into the fixtures under `backend/apps/workflow/fixtures/` with stable primary keys, so
   a fresh `make seed` reproduces the state. A state that only exists in someone's local admin
   does not exist.
5. Confirm nothing branched on a state `code` or a label:
   `rg -n 'label ==|\.code == "' backend/apps/` should return nothing about states. If it does, that
   code is the bug, not the new state.
6. Add an integration test that an illegal transition into the new state raises
   `TransitionNotAllowed`, and a legal one succeeds and writes its `ActivityRecord`.

## 9. How to add a prioritization signal

One class plus one registry entry. The evaluator is never edited. Owning skill:
`.agents/skills/prioritization-engine/`.

1. Create `backend/apps/prioritization/domain/signals/<code>.py` with
   `evaluate(self, data: SignalInput) -> SignalResult`, returning `(score_0_1, reason)`.
   `score_0_1` is clamped to `[0.0, 1.0]`; `reason` is a sentence a human can read in the UI.
   No `datetime.now()`, no randomness, no LLM — time arrives as `data.now`.
2. Register it with `@register("<code>")`. The `code` is the key used in `PriorityPolicy.weights`
   and in every persisted `breakdown`, so it never changes once a score has been written with it.
3. Create a **new** `PriorityPolicy` version carrying the weight, rebalancing the others to sum
   to 1.0 before modifiers, and move `is_active` to it. Never mutate the active row: existing
   `PriorityScore` rows keep their `policy_version` and must stay reproducible.
4. Write the database-free test in `backend/apps/prioritization/tests/domain/`, table-driven over the
   boundaries, asserting the reason string as well as the number.
5. Run `pytest backend/apps/prioritization -k <code>`, `make lint`, then `make recompute` so persisted
   scores reflect the new policy version.

A risk criterion follows the same shape: one `Specification` subclass, a `flag_code`, a severity,
one `@register_risk` line. If you are editing an existing `if`, stop — the design is wrong.

## 10. How to add an event

Owning skill: `.agents/skills/event-driven-flow/`.

1. Name the topic `<entity>.<event>` or `<entity>.<aspect>.<event>` — lowercase, dot-separated,
   past tense — and add it to the topic list in `docs/ARCHITECTURE.md` §6 **in the same commit**.
   An undocumented topic is invisible to whoever writes the next consumer.
2. Declare the payload as a `pydantic.BaseModel` in `backend/apps/<context>/domain/events.py`, `version: 1`.
   `entity.id` is the business code (`PRJ-01`), never a primary key. Adding an optional field
   keeps the version; removing or retyping one means `version: 2` and a consumer that handles
   both until nothing emits 1.
3. Emit it from the application service, inside the same `transaction.atomic()` as the mutation
   and the `ActivityRecord`. Services write `OutboxEvent`; only the relay talks to Redis.
4. Decide which existing consumer group reacts (`priority-recalculator`, `risk-evaluator`,
   `sse-fanout`, plus the `ProjectSnapshot` rebuild). One group per reason to react, never one
   per topic — a failing group must not block the others.
5. Make the handler idempotent: claim `(event_id, consumer_group)` in `ProcessedEvent` inside the
   same transaction as the effect, return early on a duplicate, and `XACK` on both paths.
6. Decide whether the browser needs it. If yes, add the topic to the `sse-fanout` allowlist *and*
   to the shared store the Astro islands subscribe to. If nothing in the UI changes, leave it out
   rather than publishing noise the client discards.
7. Add the delivery tests: one `OutboxEvent` row per service call and zero after a rollback; the
   relay puts the envelope on `aztec.events` intact; the handler run twice with the same
   `event.id` produces one effect and two acks.

## 11. Who to hand a task to

Eight subagents in `.claude/agents/`. Pick by the artifact being changed, not by the topic of the
conversation. Each one hands work back rather than crossing into another's files.

| Agent | Hand it this |
|---|---|
| `domain-architect` | A model, migration, taxonomy, workflow graph, `ActivityRecord` verb, typed domain error, risk specification, or a layering violation to review. |
| `api-engineer` | A route under `backend/apps/*/api/`, a request/response schema, pagination or filtering, the domain-error-to-HTTP mapping, CORS, or the HTTP contract of `GET /api/stream`. |
| `event-bus-engineer` | Anything between a committed transaction and a byte on the wire: `OutboxEvent`, the relay, consumer groups, `ProcessedEvent`, retries, the DLQ, SSE fan-out. "The event never arrived." |
| `prioritization-engineer` | A signal strategy, `PriorityPolicy` weights or version, the `breakdown`, `PriorityOverride`, a risk specification's severity, derived health. "Why is this project ranked first?" |
| `astro-frontend-engineer` | Anything under `frontend/`: pages, islands, the shared `EventSource` store, the typed API client, loading/empty/error/disconnected states. "The frontend does not update." |
| `seed-data-engineer` | Fixtures under `backend/apps/*/fixtures/`, `backend/scripts/xlsx_to_fixtures.py`, the `make seed` target. "loaddata fails", "seed is not idempotent", "the spreadsheet changed." |
| `test-engineer` | New coverage, `factory_boy` factories, the four integration tests (illegal transition, seed idempotency, consumer idempotency, outbox delivery), or a `make test` failure that lives in test code. |
| `devops-engineer` | Dockerfiles, Compose services, healthchecks, startup ordering, `.env.example`, `Makefile` targets, the README bring-up section, SSE not streaming through the server. |

If a task spans two of them, split it at the handoff the agents already define — for example
`domain-architect` finishes the service and the error, then `api-engineer` exposes it.

## 12. Definition of done

A change is done when all of these hold. Not four out of five.

- [ ] `make test` passes. New pure logic (signals, specifications) has database-free tests; new
      transitions, consumers and services have the integration test that proves the invariant.
- [ ] `make lint` passes: ruff check, ruff format, mypy strict over `domain/` and `services/`.
- [ ] Pre-commit ran on every commit, including `uv lock --check`. No `--no-verify` anywhere in
      the branch.
- [ ] Docs updated in the same commit when the change touched a documented topic: a model or
      invariant → `docs/ARCHITECTURE.md`; a new event topic → its §6 list; a rule of work →
      this file; product scope → `PRODUCT.md`.
- [ ] Every dependency added through `uv add` / `uv add --project backend --dev` / `npx astro add` /
      `npm install`. `git diff` shows no hand-written version string in `pyproject.toml`,
      `package.json` or a lockfile.
- [ ] `make seed` still runs twice with an identical result, if fixtures or models changed.
- [ ] No new business enum in Python, no logic branching on a label, no direct `workflow_state`
      assignment, no Redis import under `services/`.
