# Architecture — Aztec Ops

> Normative reference. Subagents and skills are derived from this document.
> If the code and this document disagree, one of them gets fixed — neither gets ignored.

## 1. What the system is for

A portfolio management system for an operation running many fronts at once.
It is not a Jira clone. It is an **operational command center** that answers three questions
every morning:

1. What should be worked on today, and why exactly that?
2. What is at risk, blocked, or has no clear next step?
3. Who is overloaded?

Minimum requirements from the challenge:

- create and update projects
- store owner, status, priority, due date, next step, blockers and notes
- detect projects at risk, blocked, or without a clear next step
- provide a useful view for operational follow-up
- expose an explicit, explainable prioritization criterion

## 2. Architecture decisions

| # | Decision | Why | Rejected alternative |
|---|---|---|---|
| 1 | Django 6 + django-ninja | Admin comes free for managing taxonomies (states, priorities, workflows) without writing CRUD; Ninja gives a typed API with Pydantic schemas | DRF — more ceremony, weaker typing |
| 2 | PostgreSQL | Strong transactions for the outbox pattern; JSONB for event payloads and score breakdowns | SQLite — no useful `SELECT FOR UPDATE SKIP LOCKED` |
| 3 | Transactional Outbox, drained onto Celery | The event is written in the SAME transaction as the state change. No dual-write, no lost events. A drain task dispatches to a handler registry, so at-least-once fan-out costs no process and no producer ever names a consumer | Publishing straight from the service — events vanish if Redis is down right after commit. Redis Streams — real consumer groups, but a second job system beside Celery's ([ADR 0010](adr/0010-celery-as-the-bus.md)) |
| 4 | SSE, not WebSockets | The flow is one-way, server to client. SSE reconnects on its own, survives proxies, and is trivial to demo | WebSockets — bidirectionality we do not need |
| 5 | Deterministic, versioned prioritization | An operational ranking must be auditable and reproducible. Every score carries its reason breakdown | LLM-driven ranking — neither auditable nor reproducible |
| 6 | States and workflows in the database, not enums | The operation must be able to evolve; adding a state cannot require a deploy | `TextChoices` on the model |
| 7 | Django fixtures for seed data | Built into Django, versioned in git, deterministic, and `loaddata` is one command. Reviewers can read the data as JSON | A custom xlsx importer — more code to maintain and to trust |
| 8 | Astro 7 + islands | Server render for a fast first paint; only the live regions hydrate and listen to SSE | Full SPA — unnecessary weight for four views |
| 9 | Celery for the schedule **and** the bus | One job system, not two. Beat's `crontab(hour=0, minute=0)` replaces a loop that had to remember the last local date; the same worker drains the outbox and dispatches handlers, so seven application processes became three | A hand-rolled ticker loop; and keeping Redis Streams beside Celery — two brokers, two retry policies, two dead-letter stories for a bus moving a few dozen events an hour ([ADR 0010](adr/0010-celery-as-the-bus.md)) |
| 10 | Risk flags computed on read, never stored | Overdue is `due_date < today` and stale is `last_activity < now - N days`; the other four are pure functions of current rows. A stored derivation can be wrong, a computed one cannot — so the table, the handler and the change event all go ([ADR 0011](adr/0011-risk-flags-computed-on-read.md)) | A `RiskFlag` table reconciled by a handler and re-checked on every clock tick — three copies of one conclusion, each able to disagree with the rows beneath it |

## 3. Domain model

### 3.1 Configurable taxonomies (`catalog` app)

Anything the operation might want to change without a deploy lives in admin-editable tables:

- `EngagementType` — Project / Recurring maintenance / Diagnostic. Carries `weight` for prioritization.
- `ProjectType` — Automation / Consulting / ... (the dataset's `project_type_api`).
- `Stage` — Execution, Discovery, etc. Ordered.
- `Priority` — Critical / High / Medium / Low, with numeric `weight` and `color`.
- `Role` — team member role.
- `Currency` — ISO-4217 code the project is billed in, with `minor_units` (the decimal places).
  A taxonomy and not a `varchar(3)` because the frontend renders a validated select from the
  served list, and `minor_units` is the one fact it cannot derive: 28000 CLP is not 28000 USD.

Every taxonomy has: `code` (stable slug, the only thing code compares against), `label`,
`order`, `is_active`, `color`, plus whatever weights the prioritization engine consumes.
The whole vocabulary is served in one document by `GET /api/v1/catalog`, so the client never
hardcodes a list of anything.
**Code never compares against labels — only against `code`.**

### 3.2 Configurable workflows (`workflow` app)

This is the Jira-like part. A workflow is a directed graph of states.

- `Workflow` — `code`, `name`, `applies_to` (`PROJECT` | `TASK`), `is_default`.
- `WorkflowState` — belongs to a workflow. Fields: `code`, `label`, `category`
  (`BACKLOG | IN_PROGRESS | BLOCKED | DONE | CANCELLED`), `is_initial`, `is_terminal`,
  `order`, `color`.
  `category` is what the rest of the system queries (is it blocked? is it closed?), never the
  specific `code`. That is what lets new states be added without touching any logic.
- `WorkflowTransition` — `from_state`, `to_state`, `label`, `requires_reason` (bool),
  `requires_fields` (fields that must be non-empty to transition, e.g. `next_step`),
  `guard` (code of a registered guard), `is_active`.
- `WorkflowBinding` — binds a workflow to an `EngagementType`, or marks the default. A
  Diagnostic can follow a different lifecycle than a Recurring maintenance engagement.

Hard rules:

- A transition can **only** run if an active `WorkflowTransition` `from → to` exists. The API
  never assigns a state directly.
- The transition service validates guards, demands `reason` when the transition requires it,
  and records the change.
- An illegal transition raises the domain error `TransitionNotAllowed` — not a 500.

### 3.3 Portfolio and work (`portfolio`, `work` apps)

- `Client` — `alias`, notes.
- There is **no person model here**. The people who own projects and are assigned tasks are
  `accounts.User` rows (§3.3b); `Project.owner`, `Task.assignee` and `Blocker.owner` are foreign
  keys to `AUTH_USER_MODEL`. What stays in `portfolio` is the *question* about a person that only
  this context can answer: `OwnerLoadRepository.load_for_codes` derives owner load from `work.Task`
  rows over `User.weekly_capacity_points`. Actual load is computed, never stored denormalized (the
  dataset's `Team` sheet is a projection, not a source of truth).
- `Project` — `code`, `name`, `client`, `engagement_type`, `project_type`, `stage`,
  `workflow_state`, `owner`, `start_date`, `target_date`, `business_value`, `currency`,
  `summary`, `next_step`, `is_archived`.
- `Task` — `code`, `project`, `assignee`, `priority`, `workflow_state`, `due_date`, `title`,
  `detail`, `last_progress`.
- `TaskDependency` — `task` → `depends_on` (FK to Task), plus `raw_label` for dataset values
  that arrive as free text and do not resolve to an existing task. Cycles are rejected.
- `Blocker` — attached to a project or a task, with `description`, `kind`
  (`EXTERNAL_DEPENDENCY | ACCESS | DECISION | TECHNICAL`), `raised_at`, `resolved_at`, `owner`.
  An open blocker is a first-class fact, not a string in a notes field.
- `Note` — chronological comments on a project or task.

Domain invariants:

- A project with no `next_step` and no task in an `IN_PROGRESS` state has **no clear next step**.
- A project is **blocked** if it has at least one open `Blocker`, or its state category is
  `BLOCKED`, or at least one of its tasks is in a `BLOCKED` state.
- A null `target_date` is not harmless missing data — it is a risk signal (`NO_TARGET_DATE`).

### 3.3b Identity (`accounts` app)

`accounts.User` extends `AbstractUser` and **is the person**. It exists on day one because
`AUTH_USER_MODEL` cannot be changed after the first migration without hand-written surgery
across every table holding a user foreign key — the cost now is zero, the cost later is a
weekend.

Beyond what `AbstractUser` supplies it carries:

- `code` — stable slug, unique, the identifier the event bus, the fixtures and every payload use
  (`camila.torres`). `username` mirrors it.
- `alias` — display name from the source data (`Camila Torres`).
- `role` — FK to `catalog.Role`, nullable, `on_delete=SET_NULL`.
- `weekly_capacity_points` — `smallint`, constrained positive, the *denominator* of owner load.

There is no separate `TeamMember`. Whoever is assigned a task is whoever signs in to move it, so
they are one entity. The earlier design gave `TeamMember` a nullable one-to-one to the account: a
nullable link that is never null lies in the schema and forces a `request.user.team_member` hop
that can be `None` at every permission check.

The cost is written down rather than hidden: identity now carries two operational attributes
(`role`, `weekly_capacity_points`). One row per person beats a nullable one-to-one that is never
null, and the alternative — a profile table joined on every read — buys purity paid for on every
query.

Two things did **not** move into `accounts`:

- **Owner load.** `weekly_capacity_points` describes the person, but the numerator is an
  aggregation over `work.Task`. That query stays in `portfolio/repositories.py` — "who is
  overloaded" is a portfolio question, and `accounts` must not learn about `work`.
- **`ActivityRecord.actor`.** It stays a string rather than a foreign key precisely so the
  prioritization engine and the event handlers can write records as `system`. A foreign key
  would force a fake user row to exist to satisfy it. `Note.author` is a string for the same
  reason, plus one more: a note must survive its author leaving the roster.

The five people in the source dataset are real assignees with no password. Seed fixtures create
them with `set_unusable_password()` — which is exactly what that method exists for — so no
credentials are invented for seed data.

### 3.4 Audit trail and timeline (`activity` app)

Explicit requirement: when a project changes state, or projects get deprioritized and
reprioritized, there must be a queryable record.

- `ActivityRecord` — append-only, never updated, never deleted:
  `entity_type`, `entity_id`, `verb` (`CREATED`, `STATE_CHANGED`, `PRIORITY_CHANGED`,
  `BLOCKER_RAISED`, `BLOCKER_RESOLVED`, `OWNER_CHANGED`, `NEXT_STEP_SET`, `TASK_ADDED`,
  `NOTE_ADDED`, `SEEDED`), `actor` (a user, or `system` when the engine caused it),
  `from_value`, `to_value`, `reason`, `metadata` (JSONB), `occurred_at`, `correlation_id`.
- Every reprioritization is recorded, distinguishing:
  - `MANUAL` — a human forced the position; reason is mandatory.
  - `POLICY` — the engine recomputed because data changed; the record names the signal that moved.
- `correlation_id` chains "deprioritize A in order to prioritize B" into a single movement, so
  the UI can show it as one decision instead of two unrelated facts.

The project timeline is `ActivityRecord` filtered by entity, and it is what the detail view renders.

## 4. Prioritization engine (`prioritization` app)

### 4.1 The criterion

A 0–100 score: the weighted sum of normalized signals. Each signal is an independent
**strategy** returning `(score_0_1, reason)`. The policy lives in the database and is
versioned: `PriorityPolicy(version, is_active, weights JSONB)`.

| Signal | Weight | What it measures |
|---|---|---|
| `deadline_pressure` | 0.25 | Days until `target_date`. Overdue = 1.0. No date = 0.5 plus a flag. |
| `overdue_work` | 0.20 | Ratio of overdue tasks to open tasks. |
| `criticality` | 0.15 | Volume of open Critical/High tasks. |
| `business_value` | 0.15 | Contract value, normalized on a log scale — 28k does not deserve 3.5× the operational attention of 8k. |
| `blockage` | 0.15 | Open blockers and how old they are. An old blocker raises the score: it needs intervention, not patience. |
| `staleness` | 0.10 | Days without recorded activity, plus absence of `next_step`. |

Modifiers adjust the result rather than adding to it:

- `engagement_type.weight` — a Diagnostic near its deadline does not compete on the same
  footing as recurring maintenance.
- Owner saturation: if the owner is already over capacity the project is flagged
  `OWNER_OVERLOADED`. The score is not lowered. Priority belongs to the work, not to who
  happens to be free.

Every `PriorityScore` persists `value`, `policy_version`, `breakdown` (JSONB: each signal, its
raw value, its weight and its human-readable reason) and `computed_at`. The UI shows the "why"
next to the number. That is what makes the ranking defensible.

### 4.2 When scores are recomputed

Recomputation is automatic. The manual path exists only for two bootstrap cases: right after
seeding, and after activating a new `PriorityPolicy` version, when every score must be rebuilt
against new weights. It is never part of normal operation, and it is deliberately not a management
command — a command run from a laptop against production is an accident waiting. It is the
**"Recompute priority for selected projects"** admin action, or
`POST /api/v1/projects/{code}/recompute` and `POST /api/v1/recompute`. The seeding step that runs
during `make up` chains it, so a freshly started stack is already scored.

There are two triggers, because there are two ways a score can go stale.

**Data changed.** Any project mutation writes an `OutboxEvent`; the `priority-recalculator`
handler picks it up and recomputes that one project, then emits
`project.priority.recalculated`, which reaches the browser through `sse-fanout`. This path is
covered by the event flow in §6 and needs nothing extra.

**Time passed.** `deadline_pressure` and `staleness` are functions of *now*, not of any event. A
project can cross its target date, or go stale, without a single mutation. Nothing in an
event-driven system notices that on its own, so a clock has to be an explicit participant:

- Celery Beat holds the schedule: `events.emit_interval_tick` every `TICKER_INTERVAL_SECONDS`
  (default 5 minutes) and `events.emit_day_boundary_tick` at `crontab(hour=0, minute=0)`. Beat
  only schedules; the `worker` service runs the task. The task writes a `clock.ticked`
  `OutboxEvent` and returns — the clock is a producer like any other, and it goes through the
  outbox and the drain exactly like a transition does.
- Beat rather than a loop because the crontab entry removes the in-process date state: a loop had
  to remember the last local date it saw to detect a rollover, and that state is wrong after every
  restart and duplicated the moment a second replica exists.
- Recomputing all 22 projects on every tick would work at this size and would be the wrong shape
  at any other. Instead each `PriorityScore` persists `valid_until`: the earliest future instant
  at which any time-dependent signal would change bucket — the target date itself, the start of
  the final week, or the staleness threshold, whichever comes first.
- The `priority-recalculator` handler reacts to `clock.ticked` by selecting only the projects
  where `valid_until <= now`, and recomputing those. On a quiet tick that query returns nothing
  and the tick costs one index scan.

So a project that becomes overdue at midnight is re-scored, re-flagged, and pushed to any open
browser within one tick, with no one running a command and no page refresh.

The cost accepted: a score can be at most one tick out of date with respect to time. For a
board read once a morning to decide a day's work, five minutes of clock drift is not a
decision-changing error. Data changes, which are the ones a human just made and is watching for,
propagate immediately rather than on the tick.

### 4.3 Manual override

`PriorityOverride(project, position | boost, reason, actor, expires_at)`. Reason is mandatory.
It emits an `ActivityRecord` with verb `PRIORITY_CHANGED` and origin `MANUAL`, and the UI
labels it as an override — it is never disguised as a computed score.

## 5. Risk detection (Specification pattern)

Each risk condition is a composable specification supporting `and` / `or` / `not`:

- `IsBlocked` — open blocker, or state/tasks in the `BLOCKED` category.
- `IsOverdue` — `target_date` in the past, or overdue tasks.
- `HasNoNextStep` — no `next_step` and no task in progress.
- `HasNoTargetDate` — an active project with no due date.
- `IsStale` — no activity for N days (configurable).
- `OwnerOverloaded` — owner load above capacity.

The evaluator returns a list of `RiskFlag` values with severity. A project's health is **derived**
from its flags — not a field someone edits by hand.
Adding a new risk criterion is one class plus one registry line. Nothing else changes.
(Open/closed.)

**Nothing here is persisted** ([ADR 0011](adr/0011-risk-flags-computed-on-read.md)). There is no
`RiskFlag` table, no `health` column and no `project.risk.changed` topic. Every one of the six
conditions is a pure function of rows that already exist, over a clock, so the specifications are
evaluated wherever the flags are read:

- `GET /api/v1/projects/{code}` evaluates them from the write side, at the request's instant.
- `GET /api/v1/queue` evaluates them from the `ProjectSnapshot` row it already loaded — the read
  model stores the *facts* (state category, dates, next step, task and blocker counts, owner load,
  last activity) and the conclusion is drawn per row, in Python, with no query inside the loop.

The consequence, taken deliberately: a derived value computed on read has no previous set to
compare against, so it has **no change event**. Nothing pushes "this project just became at risk";
the flags ride along in every project payload and are correct the moment anyone loads them. The
score is the deliberate exception and stays persisted — not for speed, but because
`ActivityRecord` records `PRIORITY_CHANGED` with a before and an after, and without a stored
previous value there is no before, no event and no live reprioritization.

## 6. Event-driven flow

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
  events.drain_outbox   (Celery task, on the one `worker` process)
    · kicked by transaction.on_commit; swept by Beat every EVENT_DRAIN_INTERVAL_SECONDS
    · claims rows with SELECT ... FOR UPDATE SKIP LOCKED, marks them published
    · asks the registry which handlers subscribe to the topic — the producer never knew
        │  one events.handle_event(handler_name, event_id) per subscribed handler
        ▼
  Celery broker (Redis)  ──┬── handler: priority-recalculator
                           │      └─> emits project.priority.recalculated
                           ├── handler: snapshot-builder
                           │      └─> rebuilds ProjectSnapshot (emits nothing)
                           └── handler: sse-fanout
                                  └─> PUBLISH aztec.sse
                                            │
                                            ▼
                                  GET /api/stream  (ASGI, async)
                                            │  text/event-stream
                                            ▼
                                  Astro island (EventSource) → live state patch
```

Each `handle_event` task claims `(event_id, handler)` in `ProcessedEvent` and runs the handler in
the same transaction, so the claim is durable only if the effect is. Retries and backoff are
Celery's; past `EVENT_MAX_ATTEMPTS` the *outbox row itself* is dead-lettered — `dead_lettered_at`
and `last_error` are set on the row that already exists, and the admin can re-queue it.

A handler is a function in `apps/<context>/handlers.py` decorated with
`@register_handler(name=..., topics={...})`. That module name is fixed: app-ready autodiscovers
exactly `handlers`, and a reactor declared anywhere else is never imported and silently never runs.

Stable event envelope:

```json
{
  "id": "uuid",
  "topic": "project.state_changed",
  "occurred_at": "2026-07-28T10:00:00Z",
  "actor": "camila",
  "correlation_id": "uuid",
  "entity": {"type": "project", "id": "PRJ-01"},
  "payload": {"from": "execution", "to": "blocked", "reason": "..."},
  "version": 1
}
```

Topics: `project.created`, `project.updated`, `project.state_changed`,
`project.priority.recalculated`, `task.created`, `task.updated`,
`task.state_changed`, `blocker.raised`, `blocker.resolved`, `note.added`, `clock.ticked`. Payload schemas live in
`docs/EVENTS.md` §4.

Rules:

- Handlers are **idempotent**, deduplicating on `(event.id, handler)` via a `ProcessedEvent` table.
  At-least-once means the same event will arrive twice; the handler must survive that.
- A failing handler does not block the others — one delivery task per handler per event, so they
  retry and fail independently.
- Retries use backoff (roughly 1s, 2s, 4s, 8s, 16s with jitter); after `EVENT_MAX_ATTEMPTS` the
  outbox row is dead-lettered and shows up in the admin, where it can be re-queued. An event dying
  silently is worse than a loud error.
- Application services **never** publish. They only write to the outbox — they do not call
  `.delay()` either, because naming a task is naming a consumer.
- `sse-fanout` is the one handler permitted to hold a Redis client: publishing *is* its effect, so
  there is no database write for it to be inconsistent with.

## 7. Layers, and where each thing goes

```
backend/apps/<context>/
  domain/          # pure logic. No django.db imports. Testable with no database.
                   #   specifications.py, policies.py, value_objects.py, events.py, errors.py
  models.py        # persistence (Django ORM) + the named queries, as QuerySet/Manager methods.
                   #   No business rules.
  repositories.py  # rare. Only a query that spans contexts and so belongs to no single model.
  services/        # use cases. Orchestrate queries + domain + outbox + activity. Transactional.
  api/             # ninja routers + schemas. Translates HTTP ↔ services. Zero logic.
  admin.py         # admin configuration
  handlers.py      # event reactors (only in contexts that react). The name is fixed: app-ready
                   #   autodiscovers exactly `handlers`, so a reactor elsewhere never runs.
```

**Named queries live on the model's `QuerySet`, exposed through its `Manager`.** A query is a
method on the queryset of the model that owns the rows, and the manager is built with
`QuerySet.as_manager()` — or `Manager.from_queryset(...)` when the manager also needs behaviour
that is not a query. Every method returns the queryset type, so the vocabulary composes:

```python
Task.objects.assigned_to(user).open().overdue(as_of=today)   # one lazy query, filters ANDed
```

A module-level `open_tasks_for(user) -> list[Task]` cannot be narrowed further, so every new
combination needs a new function — the same combinatorial fragmentation that keeps row-to-value-
object converters off free functions. A method that must materialise (a count, an aggregate, a
dict) is fine, but its docstring says so, because it ends the chain.

`repositories.py` survives for **exactly one case**: a query that spans contexts and therefore
belongs to no single model. Owner load is the example — it aggregates `work.Task` keyed by user
against `accounts.User.weekly_capacity_points`, and `accounts` must never learn that `work.Task`
exists. Such a module lives in the context that *consumes* the query, not the one that owns the
rows. Everything else goes on the manager.

Dependency rules — enforced, not suggested:

- `domain/` imports neither Django nor other apps.
- `api/` does not import `models`; it talks to `services/`.
- `services/` does not import `api/`. It may call a manager directly.
- Contexts talk to each other through events or through the published queries of the owning
  model's manager, never through a hidden FK into another context's internal models.

## 8. Read side (CQRS-lite)

The command center needs a heavy join: project + score + owner load + task and blocker counts.
Resolving that through the ORM on every request does not hold up.

`ProjectSnapshot` is a denormalized read model rebuilt by the `snapshot-builder` handler whenever
any event for that project arrives. The read API queries only that table. Write and read sides
evolve independently, and the operational view loads in a single query.

**It denormalizes facts, never conclusions** ([ADR 0011](adr/0011-risk-flags-computed-on-read.md)).
There are no `risk_flags` and no `health` columns: those are derived from the row's own counts and
dates on every read, so a row nobody has rebuilt since yesterday still reports today's truth. What
it stores is what is expensive to aggregate — the score copy, `open/overdue/blocked/urgent/
in_progress_task_count`, `open_blocker_count`, `oldest_blocker_age_days`, `owner_load_points`,
`last_activity_at` — and each of those is a fact about rows, not a judgement about them.

The two derived facets pay for this. `?health=` and `?risk_flag=` cannot be `WHERE` clauses, so
`read_queue` evaluates the ordered match and filters it in Python; at 22 projects that is free, and
the alternative — the six specifications written a second time as SQL predicates — is the drift the
whole decision removes.

## 9. Frontend (Astro)

- `/` — **command center**: prioritized queue with score and its breakdown, risk indicators,
  open-blockers panel, no-next-step panel, load per person.
- `/projects/{code}` — detail: fields, tasks, blockers, transition buttons (only the legal
  transitions for that workflow), and the activity timeline.
- Islands hydrate only where data is live. One shared `EventSource` connection behind a store;
  components subscribe to the store, never open their own connection.
- Mandatory states on every view: loading, empty, error, and SSE-disconnected with a visible
  retry. A UI that only handles the happy path is not usable for running an operation.

## 10. Seed data (Django fixtures)

The spreadsheet is converted once into Django fixtures committed to the repository under
`backend/apps/*/fixtures/` (or a single `seed/` app). A developer-only script,
`backend/scripts/xlsx_to_fixtures.py`, regenerates them from the original `.xlsx` when the source data
changes; it is not part of the runtime path.

Seeding is not a separate step. The `api` container migrates and then runs `manage.py seed` —
`loaddata catalog workflows portfolio work activity`, plus the code sequences, the scores and the
credentials — before uvicorn binds, so `make up` is the whole bootstrap. It also means
`DJANGO_SUPERUSER_USERNAME`, `DJANGO_SUPERUSER_PASSWORD`, `DJANGO_SUPERUSER_EMAIL` and
`SEED_USER_PASSWORD` must be set in `.env` before the first `make up`; they ship empty and an empty
value seeds a database nobody can sign in to.

Rules:

- Fixtures use explicit stable primary keys, so `loaddata` is an upsert: bringing the stack up
  twice leaves the database identical. That is what makes seeding on every start safe.
- Fixture generation resolves the dataset's rough edges up front: `'None'` strings become real
  nulls, the free-text `blockers` column becomes typed `Blocker` rows, and `dependency` text is
  matched against task titles within the same project, falling back to `raw_label`.
- The `Team` sheet counters are not imported. They are a projection of the task data and are
  recomputed by the system.
- After seeding, scores are computed by `recompute_active_portfolio()`, which `seed` calls as
  its last step (§4.2). Risk flags need no such step: they are computed on read, so a freshly
  seeded portfolio is correctly flagged before anything has run (§5).

### 10.1 What the source data actually contains

Measured from the spreadsheet (`data/raw/dataset.json` holds the normalized export):

- 22 projects, 82 tasks, 5 team members, 16 distinct clients.
- `engagement_type`: Diagnostico, Mantenimiento o recurrente, Proyecto.
- `project_type_api`: Automatizacion, Consultoria. `stage`: Descubrimiento, Ejecucion.
- `status` is `Activo` for **every** project — the source has no project lifecycle variety.
  The project workflow state is therefore seeded from `stage`, and the extra states
  (Paused, Blocked, Done, Cancelled) exist in the workflow and are exercised by the demo
  fixtures so the challenge's "projects in different states" requirement is actually met.
- `health`: Sano, En riesgo, Bloqueado. Imported only as a cross-check against what our own
  risk specifications derive — the derived value is the one the system uses.
- Task `status`: Por hacer, En progreso, En revision, Bloqueada. There is not a single
  completed task: the dataset is pure open backlog. `Hecha` exists in the task workflow anyway.
- Task `priority`: Critica, Alta, Media, Baja.
- 61 of 82 tasks carry a free-text `dependency`; 5 of 22 projects have no `target_date`, which
  is what makes `NO_TARGET_DATE` fire on real rows rather than on invented ones.

## 11. Quality

- `pytest` + `pytest-django`, `factory_boy` factories.
- The prioritization engine and the risk specifications are tested **without a database** —
  they are pure. That is where coverage should be high.
- Integration tests for: illegal transitions, seed idempotency, handler idempotency, and
  outbox → event actually delivered.
- `ruff` (lint + format) and `mypy` in strict mode over `domain/` and `services/`.

## 12. Deliberately out of scope

Documented in the README rather than hidden:

- **Multi-tenancy.** One operation, one portfolio. Authentication is *not* on this list any more:
  the `X-Actor` header was replaced by JWT access tokens (`docs/API.md` §1.2), because a header
  anyone can type does not merely leave permissions unimplemented — it leaves them
  unrepresentable, and it makes `ActivityRecord.actor` a claim rather than a fact. What remains
  out of scope is server-side token revocation: there is no blacklist table, logout clears the
  cookie, and rotating `DJANGO_SECRET_KEY` invalidates every outstanding token at once.
- External notifications (Slack, email). The bus already exists — it would be one more registered handler.
- Task drag & drop. Transitions happen through buttons that respect the workflow.
- Historical metrics / burndown. `ActivityRecord` already stores the raw material for them.
