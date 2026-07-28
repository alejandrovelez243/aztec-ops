# Aztec Ops

Aztec Ops is an operational portfolio manager for a services team running many client
engagements at once. It takes 22 active projects, 82 open tasks and 5 people, and turns them
into a defensible daily decision by answering three questions every morning: what should be
worked on today and why exactly that; what is at risk, blocked, or has no clear next step; and
who is overloaded. The ranking is a deterministic 0–100 score with a persisted per-signal reason
breakdown — no LLM in the ranking, no opinion, no unexplained number. Stack: Django 6 +
django-ninja + PostgreSQL 16 + Redis Streams (transactional outbox) + SSE + Astro 7, on Docker
Compose.

---

## Contents

- [Quick start (Docker Compose)](#quick-start-docker-compose)
- [The prioritization criterion](#the-prioritization-criterion)
- [How the domain is modelled](#how-the-domain-is-modelled)
- [The event-driven path](#the-event-driven-path)
- [The audit trail](#the-audit-trail)
- [Repository map](#repository-map)
- [How this repository was built with AI tooling](#how-this-repository-was-built-with-ai-tooling)
- [Deliberately out of scope](#deliberately-out-of-scope)

| Document | What it answers |
|---|---|
| [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) | The normative spec: decisions, domain model, layers, event flow |
| [`docs/DATA_MODEL.md`](docs/DATA_MODEL.md) | Every table, field, constraint and index |
| [`docs/API.md`](docs/API.md) | The HTTP contract the frontend consumes, including SSE |
| [`docs/EVENTS.md`](docs/EVENTS.md) | The canonical event catalog — new topics get registered here |
| [`docs/CONTRIBUTING.md`](docs/CONTRIBUTING.md) | Setup, dependency policy, Git Flow, how to add a use case |
| [`docs/RUNBOOK.md`](docs/RUNBOOK.md) | Operating it, and what to do when it breaks |
| [`docs/adr/`](docs/adr/) | Why each decision was made, and what it cost |
| [`PRODUCT.md`](PRODUCT.md) · [`DESIGN.md`](DESIGN.md) · [`CLAUDE.md`](CLAUDE.md) | Product truth, visual system, and the rules AI tooling loads every session |

---

## Quick start (Docker Compose)

```bash
git clone git@github.com:alejandrovelez243/aztec-ops.git
cd aztec-ops
cp .env.example .env
make up
docker compose exec api python manage.py migrate
make seed
```

`make up` starts eight services: `postgres`, `redis`, `api` (Django on ASGI), `relay` (outbox
relay), `worker` (Redis Streams consumer groups — the event bus, `manage.py run_consumer`), `beat`
(Celery Beat: holds the schedule, executes nothing), `celery-worker` (executes the scheduled Celery
tasks — only ever the clock ticks that emit `clock.ticked`), `frontend` (Astro). `worker` and
`celery-worker` are different processes and are never interchangeable. `make seed` runs
`loaddata catalog workflows portfolio work activity` followed by `make recompute`, which
computes `PriorityScore`, risk flags and `ProjectSnapshot`. Fixtures use explicit stable primary
keys, so running `make seed` twice leaves the database identical.

| URL | What it is |
|---|---|
| http://localhost:4321 | Web UI — the command center and project detail views |
| http://localhost:8000/api/docs | OpenAPI docs for the django-ninja API |
| http://localhost:8000/admin/ | Django admin — taxonomies, workflows, transitions, outbox, DLQ |
| http://localhost:8000/api/health | Liveness probe |

The seed creates a local superuser: **`admin` / `aztec-ops`**. It exists only to make the admin
reachable in a five-minute review; it is a fixture, not a credential to reuse anywhere.

Then:

```bash
make logs     # docker compose logs -f api relay worker
make test     # pytest inside the api container
make lint     # ruff check + ruff format --check + mypy
make reset    # drop volumes, migrate, seed from scratch
make down     # stop, keeping volumes
```

Management commands must run inside the `api` container (`docker compose exec api ...`) so they
resolve `postgres:5432` and `redis:6379` on the compose network.

### Without Docker

You still need PostgreSQL 16 and Redis 7 reachable locally, and **Node 22.12 or newer** — Astro 7
refuses to build on anything older. Python is 3.12, managed with `uv`.

```bash
uv sync                                   # backend deps from uv.lock
uv run pre-commit install                 # ruff check --fix, ruff format, uv lock --check
uv run python manage.py migrate
uv run python manage.py loaddata catalog workflows portfolio work activity
uv run python manage.py recompute
uv run uvicorn config.asgi:application --port 8000
uv run python manage.py run_outbox_relay  # separate shell
uv run python manage.py run_consumers     # separate shell — Redis Streams consumer groups
uv run celery -A config beat              # separate shell — the schedule
uv run celery -A config worker            # separate shell — runs the scheduled tasks

cd web && npm install && npm run dev      # http://localhost:4321
```

Do not serve `/api/stream` from `runserver`: WSGI buffers the response and SSE looks broken when
the code is fine. Use an ASGI server.

Dependencies are only ever added through a CLI — `uv add`, `uv add --dev`, `uv remove`,
`npm create astro@latest`, `npx astro add`, `npm install`. Hand-editing `pyproject.toml`,
`package.json` or a lockfile to add a dependency is forbidden; it is how a project silently
drifts off current versions. `pre-commit` is never bypassed with `--no-verify`.

---

## The prioritization criterion

The queue is ordered by a score from 0 to 100. The score is the weighted sum of six independent
signals, each normalized to 0–1 and each returning a sentence explaining what it saw. The weights
live in the database (`PriorityPolicy.weights`, a versioned JSONB row), not in code, so the
operation can rebalance them without a deploy — and every score that was ever computed keeps the
`policy_version` it was computed under, so old rankings stay reproducible.

| Signal | Weight | What it measures | Why that weight |
|---|---|---|---|
| `deadline_pressure` | 0.25 | Days until `target_date`. Already overdue scores 1.0. No date at all scores 0.5 and raises a `NO_TARGET_DATE` flag. | A commitment with a date is the only hard external constraint in the portfolio, so it is the single largest term. |
| `overdue_work` | 0.20 | Overdue tasks divided by open tasks. | A project can be inside its deadline and still be rotting underneath; the task level detects that earlier than the project level. |
| `criticality` | 0.15 | Volume of open tasks whose priority code is critical or high. | Severity of the work already classified by the team, but it is self-reported, so it does not outrank the dates. |
| `business_value` | 0.15 | Contract value, normalized on a log scale. | Money should order projects, not dominate them. See below. |
| `blockage` | 0.15 | Number of open blockers, weighted by the age of the oldest. | A blocker is the one condition no amount of individual effort clears; it needs a human decision today. |
| `staleness` | 0.10 | Days with no `ActivityRecord`, plus absence of a `next_step`. | Silence is real evidence but it is weak evidence, so it breaks ties rather than setting the order. |

The weights sum to 1.0. Modifiers then multiply the result instead of adding to it — currently
only `engagement_type.weight`, so a Diagnostico near its deadline does not compete on the same
footing as a Mantenimiento o recurrente engagement.

### Two things that surprise people

**Business value is normalized logarithmically, not linearly.** In this portfolio the USD
contracts run from 1,000 to 38,000, and two rows are denominated in COP at 85M and 120M. Under
linear normalization those two would pin the top of the queue permanently and every other signal
would become rounding noise; even within USD alone, a 28,000 engagement would score 3.5× an 8,000
one. On a log scale, 28,000 normalizes to 0.92 and 8,000 to 0.57 — a bigger contract still ranks
higher, by a margin that a human recognizes as proportionate. Value orders the portfolio; it does
not own it. Amounts are normalized within their own currency band, so a COP figure does not
outrank every USD project by virtue of the unit it is written in.

**An old blocker raises priority instead of lowering it.** The intuitive model is that a project
stuck waiting on someone else should sink, because nothing can be done about it. That is exactly
backwards for a command center. `blockage` grows with the age of the oldest open `Blocker` and
saturates at three weeks: a blocker open for 19 days is not waiting, it is rotting, and it is the
single item on the list that only a human intervention can clear. Decaying it with age would bury
precisely the projects this system exists to surface.

### Owner saturation flags, it never subtracts

When a person's computed load exceeds their `weekly_capacity_points`, every project they own gets
the `OWNER_OVERLOADED` flag and keeps its score unchanged. Priority belongs to the work, not to
who happens to be free. Lowering the score of an overloaded owner's projects would make the
bottleneck disappear from the top of the screen at the exact moment it becomes the operation's
main problem. The flag turns it into what it actually is — a staffing decision, taken by a person
who can see it. In this dataset that is visible immediately: Camila Torres carries 28 open tasks
across 7 projects, 20 of them high or critical, against Santiago Vera's 4.

### Manual override

`PriorityOverride(project, position | boost, reason, actor, expires_at)` lets a human force a
position. `reason` is mandatory and non-empty, and the override is stored separately — it is
never written into `PriorityScore.value`. The computed score stays visible beside it and the UI
labels the row as an override, so it never masquerades as a computed result. The reason is a
database constraint rather than a convention because a forced position with no recorded
justification is indistinguishable from a bug three weeks later, and the whole point of the
ranking is that it can be defended to someone who was not in the room. Every override writes an
`ActivityRecord` with verb `PRIORITY_CHANGED` and origin `MANUAL`; engine recomputations use
origin `POLICY` and name the signal that moved.

### A worked example: PRJ-01

Global Contract Management for Atlas Foods, owned by Daniel Rojas. Real row from the dataset,
scored under policy v1 as of 2026-07-28:

| Signal | Raw | × Weight | Contribution | Reason |
|---|---|---|---|---|
| `business_value` | 0.92 | 0.15 | 13.7 | 28,000 USD, near the top of the USD band. |
| `blockage` | 0.86 | 0.15 | 12.9 | 2 open blockers; the oldest raised 2026-07-10, open 18 days. |
| `deadline_pressure` | 0.50 | 0.25 | 12.5 | No `target_date` recorded — neutral value plus `NO_TARGET_DATE`. |
| `overdue_work` | 0.50 | 0.20 | 10.0 | 2 of 4 open tasks are past due. |
| `criticality` | 0.60 | 0.15 | 9.0 | 3 of 4 open tasks are Critica or Alta. |
| `staleness` | 0.50 | 0.10 | 5.0 | No `next_step` recorded; one task in progress. |
| | | **subtotal** | **63.1** | |
| modifier | ×1.1 | | **69** | engagement type Proyecto. |

Risk flags: `BLOCKED` (open blockers, and PRJ-01-T03 sits in a `BLOCKED` state category),
`NO_TARGET_DATE`, `IS_OVERDUE`. Not flagged `OWNER_OVERLOADED`: Daniel Rojas holds 11 open tasks
across 3 projects and is inside capacity — the constraint here is the external dependency, not
the person.

Read out loud, that is: *PRJ-01 scores 69. It is not first because it is expensive; it is there
because it has been blocked on an external dependency for 18 days, half its open work is already
past due, and nobody ever gave it a due date.* The `breakdown` JSONB on `PriorityScore` stores
exactly the table above, so the UI renders the justification next to the number rather than
reconstructing it.

---

## How the domain is modelled

**Projects and tasks.** A `Project` carries `code`, `client`, `engagement_type`, `project_type`,
`stage`, `workflow_state`, `owner`, `start_date`, `target_date`, `business_value`, `summary` and
`next_step`. A `Task` carries `code`, `project`, `assignee`, `priority`, `workflow_state`,
`due_date`, `title`, `detail` and `last_progress`. Two things that are usually strings in a notes
field are first-class rows here: `Blocker` (attached to a project or task, with `kind`,
`raised_at`, `resolved_at`, `owner`) and `TaskDependency` (a real FK to another task, with
`raw_label` preserved for the free-text dependencies that do not resolve — 61 of the 82 tasks
carry one). Team load is computed from tasks, never stored: the dataset's `Team` sheet counters
are a projection, so they are deliberately not imported.

**Configurable, Jira-style workflows.** A `Workflow` is a directed graph of `WorkflowState`
rows, bound to an engagement type by `WorkflowBinding`, so a Diagnostico can follow a different
lifecycle than a Mantenimiento o recurrente engagement. A state carries a `code`, a Spanish
`label`, and a `category` (`BACKLOG | IN_PROGRESS | BLOCKED | DONE | CANCELLED`). Movement
happens only along an active `WorkflowTransition`, which can declare `requires_reason`,
`requires_fields` (for example, you cannot leave a state without setting `next_step`) and a
`guard`. No API route assigns `workflow_state` directly; an illegal move raises
`TransitionNotAllowed`, not a 500.

**Why states are data, not code.** The rest of the system queries `category`, never a specific
state code, so adding "Esperando cliente" is an admin form submission rather than a migration, a
deploy and a scan for every `if status ==` in the codebase. The same holds for priorities,
engagement types and stages: they live in the `catalog` app with a stable `code`, an editable
`label` and their prioritization `weight`. Code compares against `code` and `category` — never
against labels, which are Spanish user-facing data.

## The event-driven path

A state change is a single database transaction that mutates the aggregate, appends the
`ActivityRecord` and writes an `OutboxEvent` — all three commit together, so an event can never
be lost or invented by a crash between two systems. A separate relay process is the only thing
allowed to publish: it drains the outbox with `SELECT ... FOR UPDATE SKIP LOCKED` and `XADD`s to
the `aztec.events` Redis stream, where three independent consumer groups pick it up. Consumers
are idempotent and deduplicate on `event.id` via `ProcessedEvent`, because at-least-once delivery
means the same event will arrive twice; a failing group does not block the others, and an event
that exhausts its retries lands in `aztec.events.dlq` and shows up in the admin instead of dying
quietly.

```
POST /api/projects/{code}/transition
        │
        ▼
  Application service  ──── begin tx ───────────────────────────┐
    · validates the transition against WorkflowTransition       │
    · mutates the aggregate                                     │
    · writes ActivityRecord                                     │
    · writes OutboxEvent(topic, payload, correlation_id)        │
  ──── commit ──────────────────────────────────────────────────┘
        │
        ▼
  Outbox relay  (separate process; SELECT ... FOR UPDATE SKIP LOCKED)
        │  XADD aztec.events
        ▼
  Redis Streams  ──┬── consumer group: priority-recalculator
                   │      └─> emits project.priority.recalculated
                   ├── consumer group: risk-evaluator
                   │      └─> emits project.risk.changed
                   └── consumer group: sse-fanout
                          └─> PUBLISH aztec.sse
                                    │
                                    ▼
                          GET /api/stream  (ASGI, async)
                                    │  text/event-stream
                                    ▼
                          Astro island (EventSource) → live state patch
```

## The audit trail

`ActivityRecord` is append-only — never updated, never deleted. Each row records `entity_type`,
`entity_id`, `verb` (`CREATED`, `STATE_CHANGED`, `PRIORITY_CHANGED`, `BLOCKER_RAISED`,
`BLOCKER_RESOLVED`, `OWNER_CHANGED`, `NEXT_STEP_SET`, `TASK_ADDED`, `NOTE_ADDED`, `SEEDED`),
the `actor` (a person, or `system` when the engine caused it), `from_value`, `to_value`, `reason`,
a `metadata` JSONB payload, `occurred_at` and a `correlation_id`. The project timeline in the
detail view is just this table filtered by entity.

Reprioritizations carry a `correlation_id` because a reprioritization is rarely one fact. Pushing
one project up usually means pushing another down, and stored as two unrelated rows the record
loses the thing that mattered: the decision. The shared correlation id chains "deprioritize A in
order to prioritize B" into one movement, so the UI can render it as a single decision with one
actor and one reason, and so the same id links the change to the events it produced downstream.

---

## Repository map

```
backend/apps/
  catalog/          # editable taxonomies: EngagementType, ProjectType, Stage, Priority, Role
  workflow/         # Workflow, WorkflowState, WorkflowTransition, WorkflowBinding + transition service
  portfolio/        # Client, Project (people are accounts.User)
  work/             # Task, TaskDependency, Blocker, Note
  activity/         # ActivityRecord (append-only) and the timeline read side
  prioritization/   # signal strategies, PriorityPolicy, PriorityScore, PriorityOverride, risk specifications
  bus/              # OutboxEvent, ProcessedEvent, relay, consumer groups, SSE endpoint
  readmodel/        # ProjectSnapshot, the denormalized read side the command center queries
config/             # Django settings, ASGI entrypoint, API router assembly
frontend/                # Astro 7 frontend: command center, project detail, SSE islands
data/raw/           # dataset.json, the normalized export of the source spreadsheet
scripts/            # xlsx_to_fixtures.py — developer-only, not in the runtime path
docs/               # architecture, data model, contributing, runbook, events, API, ADRs
docker-compose.yml
Makefile
```

Inside each app: `domain/` (pure logic, no Django imports, testable with no database),
`models.py` (persistence and the named queries, as `QuerySet`/`Manager` methods),
`services/` (transactional use cases), `api/` (ninja routers and schemas, zero logic), `consumers/` (event handlers). `api/`
never imports `models`; `domain/` imports neither Django nor another app.

### Further documentation

| File | What is in it |
|---|---|
| [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) | Normative spec: decisions and their rejected alternatives, layers, the full domain model |
| [`docs/DATA_MODEL.md`](docs/DATA_MODEL.md) | Tables, fields, constraints and invariants |
| [`docs/CONTRIBUTING.md`](docs/CONTRIBUTING.md) | How to work here: dependency CLI rules, pre-commit, migrations, commits |
| [`docs/RUNBOOK.md`](docs/RUNBOOK.md) | Operating it: relay stuck, pending consumer entries, DLQ, resets |
| [`docs/EVENTS.md`](docs/EVENTS.md) | Topic catalogue and the stable event envelope |
| [`docs/API.md`](docs/API.md) | Endpoints, schemas, domain error to HTTP mapping |
| [`docs/adr/`](docs/adr/) | One file per architectural decision, with its context and consequences |

---

## How this repository was built with AI tooling

The role is an AI engineering role, so the process is part of the submission. The repository is
built to be worked on by agents as well as people, and the configuration for that is committed
rather than kept in someone's chat history.

**`CLAUDE.md`** holds the project rules an agent must not violate: dependencies only through a
CLI, no business enums in code, state changes only through transitions, every meaningful change
writes an `ActivityRecord`, events only through the outbox, idempotent consumers, the layer
boundaries, deterministic and explainable prioritization, and "a new signal or risk criterion is
one class plus one registry entry — if you had to edit an existing `if`, the design is wrong."

**Eight specialized subagents** in `.claude/agents/`, each scoped to one context so it can be
handed a task without loading the whole system: `domain-architect`, `api-engineer`,
`event-bus-engineer`, `prioritization-engineer`, `astro-frontend-engineer`, `seed-data-engineer`,
`test-engineer`, `devops-engineer`.

**Six skills**, canonical in `.agents/skills/`: `aztec-domain`, `django-clean-arch`,
`prioritization-engine`, `event-driven-flow`, `astro-sse-client`, `aztec-local-dev`. They carry
the procedural knowledge that would otherwise be re-derived every session — how to add a signal
without breaking reproducibility, how to diagnose a relay that is not publishing, why an SSE
stream that works under `curl` but not in the browser is a CORS or buffering problem.

**The canonical-skills convention.** Skills are never written into a tool directory. They live in
`.agents/skills/<name>/SKILL.md`, and `.claude/skills/` contains only symlinks pointing back at
them (`ln -s ../../.agents/skills/<name> .claude/skills/<name>`). One copy is the source of
truth, and any other agent runtime gets a symlink of its own instead of a divergent fork, so the
knowledge in this repository is not hostage to a single vendor's directory layout. Subagent
definitions cannot follow the same pattern — each tool has its own format — so they live in
`.claude/agents/` and are ported by hand.

**External tools recommended when working on this repository:**

- **`context7`** — current documentation for Django, django-ninja and Astro. Framework majors
  move; an assumed signature is how a build silently targets a version that no longer exists.
  Check the docs before writing against an API you have not used this week.
- **`impeccable`** — the design direction for the frontend. `PRODUCT.md` is written in its
  product schema, and it owns the visual system that `DESIGN.md` records.
- **`codebase-memory-mcp`** — structural code exploration. Graph queries ("who calls this",
  "what does this transition touch") before reaching for grep and reading files one at a time.
- **`magnific`** — visual assets, when any are needed.

---

## Deliberately out of scope

Stated here rather than hidden, with what each would actually take.

- **Real authentication and multi-tenancy.** Requests identify the actor with an `X-Actor`
  header and the admin uses Django auth. Doing it properly means a real user model wired to
  `accounts.User`, an organization FK on every aggregate, and a default queryset scoped by it —
  which is a data model change, not a middleware change, and would rewrite every fixture.
- **External notifications (Slack, email).** The bus already carries everything a notifier would
  need. It is one more consumer group on `aztec.events` plus a per-user subscription table
  deciding which topics reach whom.
- **Task drag and drop.** Transitions happen through buttons that render only the legal moves
  for that workflow. Drag and drop would need an optimistic client-side move, a reconciliation
  when the transition service rejects it, and a UI answer for `requires_reason` transitions that
  cannot complete without a dialogue.
- **Historical metrics and burndown.** `ActivityRecord` already stores the raw material — every
  state change with a timestamp. What is missing is a time-bucketed aggregation job and the
  charts. It was left out because the source dataset contains no completed tasks at all, so every
  burndown chart it could draw today would be a flat line.
