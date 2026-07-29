# Events — Aztec Ops

> Canonical catalog. A topic exists when it is registered **here**. `docs/ARCHITECTURE.md` §6
> keeps the narrative of the flow and points to this file for the schemas.
> If a payload in the code and a payload in this file disagree, one of them gets fixed.

The path is: an application service writes `OutboxEvent` in the same transaction as the state
change and the `ActivityRecord` → the `events.drain_outbox` Celery task claims unpublished rows
with `SELECT ... FOR UPDATE SKIP LOCKED`, marks them published and queues one
`events.handle_event(handler_name, event_id)` per **registered handler** subscribed to the topic →
`sse-fanout` does `PUBLISH aztec.sse` → `GET /api/stream` writes `text/event-stream` → the shared
`EventSource` store in the Astro islands. Services never import the Redis client and never call
`.delay()` (`CLAUDE.md` rule 4).

The drain runs on two paths that read the same table: `enqueue_event` kicks it from
`transaction.on_commit`, and Celery Beat sweeps it every `EVENT_DRAIN_INTERVAL_SECONDS`. Neither
can lose an event and a double dispatch is absorbed by `ProcessedEvent`. See
[ADR 0010](adr/0010-celery-as-the-bus.md) for why this replaced Redis Streams.

## 1. Envelope

The envelope is fixed for every topic. New information goes inside `payload`, never at the top
level.

| Field | Type | Required | Meaning |
|---|---|---|---|
| `id` | string, UUIDv4 | yes | Event identity. The deduplication key for every consumer, the `id:` field of the SSE frame, and the value the browser replays through `Last-Event-ID`. Generated when the `OutboxEvent` row is written, never by the transport. |
| `topic` | string | yes | One of the topics in §4. Immutable once released. |
| `occurred_at` | string, ISO-8601 UTC with `Z` | yes | When the change committed, not when the drain dispatched. Consumers use it to discard a stale application of an out-of-order redelivery. |
| `actor` | string | yes | Who caused the change: the actor identifier from the request (`daniel.rojas`), or the literal `system` when the prioritization or risk engine caused it. Never null — an unattributed change is a bug. |
| `correlation_id` | string, UUIDv4 | yes | Chains every event produced by one decision, including the events a consumer emits in reaction. It is what turns "deprioritize A in order to prioritize B" into a single movement in the timeline. Defaults to the request id. |
| `entity` | object | yes | `{"type": "...", "id": "..."}`. `type` ∈ `project \| task \| blocker \| note \| clock \| member`. `id` is the **business code** (`PRJ-01`, `PRJ-01-T02`), never a database primary key — a consumer in another context must never need a FK into the emitting context. |
| `payload` | object | yes | Topic-specific, versioned. Schemas in §4. |
| `version` | integer ≥ 1 | yes | Schema version **of this topic's payload**, not a global bus version. Starts at 1 and is tracked per topic. |

Wire formats:

- Outbox row: `OutboxEvent(id, topic, entity_type, entity_id, payload JSONB, actor,
  correlation_id, version, occurred_at, published_at, attempts, last_error, dead_lettered_at)`.
  The envelope is reassembled from the row by `OutboxEvent.to_envelope()`. There is no second
  copy of it anywhere: `published_at` means "dispatched to its handlers", not "moved to a queue".
- Celery task arguments: `handle_event(handler_name, event_id)`. The envelope is **not** put on
  the broker — the broker carries an id, the row carries the truth, so a redelivery cannot deliver
  a stale copy of a payload.
- SSE frame: `id: <event.id>`, `event: <topic>`, `data: <envelope JSON>`. That is why
  `es.addEventListener(topic, ...)` works client-side and why reconnecting with
  `Last-Event-ID` resumes at the right place.

## 2. Payload versioning

`version` starts at 1 per topic. Change it only for a breaking change.

**Not breaking — keep the version:**

- Adding an optional field.
- Adding a new value to a set of taxonomy or workflow `code`s. States and priorities are
  database rows (`CLAUDE.md` rule 1), so a consumer that cannot survive an unknown `code` is
  already wrong. Match on `category` where behaviour depends on the kind of state.
- Widening a numeric range, or making a required field's value more precise.

**Breaking — bump the version:**

- Removing or renaming a field.
- Changing a field's type, or its unit, or its meaning under the same name.
- Making an optional field required, or a nullable field non-nullable.
- Changing what `entity.type` / `entity.id` refer to for that topic.

Procedure for a breaking change:

1. Emit `version: 2` from the service and update this file, keeping the version 1 schema
   documented until it is retired.
2. Handlers handle both versions before the emitter changes — deploy order is handlers first.
3. Version 1 can be deleted only when no unpublished and no dead-lettered `OutboxEvent` carries
   it — one query over the one table, `WHERE version = 1 AND (published_at IS NULL OR
   dead_lettered_at IS NOT NULL)`. Check the dead-lettered rows; a re-queued dead letter is a
   version 1 event arriving after you thought version 1 was gone.

The envelope itself is not versioned per topic. Changing the envelope is a change to every
consumer at once and is out of scope for a normal feature.

## 3. Topic naming

```
<entity>.<event>              project.created, task.state_changed, blocker.raised
<entity>.<aspect>.<event>     project.priority.recalculated
```

- Lowercase, dot-separated, `snake_case` inside a segment, at most three segments.
- Entity singular. Event in past tense: the topic reports something that already committed, so
  `project.updated`, never `project.update` or `update_project`.
- The `<aspect>` segment exists for facts derived by a consumer rather than by a user action —
  `priority`. It keeps the derived topic visually separate from the write-side ones. There is only
  one, and there is deliberately no `project.risk.changed`: risk flags are computed on read
  (ADR 0011), so they have no moment of change to announce.
- A topic name is immutable once released. Renaming means introducing the new topic, emitting
  both for one release, migrating consumers, then removing the old one.
- Never encode an identifier or a state in the topic (`project.PRJ-01.updated`,
  `project.blocked` are both wrong). That belongs in `entity` and `payload`.

## 4. Topic catalog

Codes used below are workflow state `code`s, not labels: project states `discovery`,
`execution`, `paused`, `blocked`, `done`, `cancelled`; task states `todo`, `in_progress`,
`in_review`, `blocked`, `done`. Labels are Spanish operator-editable data (`Por hacer`,
`Bloqueada`) and never appear in a payload.

`SSE` below means the topic is on the `sse-fanout` allowlist and reaches the browser.

---

### `project.created`

**Emitted by** `backend/apps/portfolio/services/create_project.py` when a project is created through
`POST /api/projects` or by the seed path. Version 1.

| Field | Type | Required |
|---|---|---|
| `name` | string | yes |
| `client_alias` | string | yes |
| `engagement_type` | string (`EngagementType.code`) | yes |
| `project_type` | string (`ProjectType.code`) | yes |
| `stage` | string (`Stage.code`) | yes |
| `state` | string (initial `WorkflowState.code`) | yes |
| `owner_alias` | string | yes |
| `target_date` | string `YYYY-MM-DD` \| null | yes (nullable — null is the `NO_TARGET_DATE` signal, not missing data) |
| `business_value` | number | no |
| `currency` | string ISO-4217 | no |

**Consumed by** `priority-recalculator`, `snapshot-builder`. **SSE**: yes — the
command center gains a row.

```json
{
  "id": "6f1b6a2c-6c3e-4f7a-9c1f-2f0f1a5f77b1",
  "topic": "project.created",
  "occurred_at": "2026-07-28T08:12:04Z",
  "actor": "daniel.rojas",
  "correlation_id": "6f1b6a2c-6c3e-4f7a-9c1f-2f0f1a5f77b1",
  "entity": {"type": "project", "id": "PRJ-01"},
  "payload": {
    "name": "Global Contract Management",
    "client_alias": "Atlas Foods",
    "engagement_type": "proyecto",
    "project_type": "automatizacion",
    "stage": "ejecucion",
    "state": "execution",
    "owner_alias": "Daniel Rojas",
    "target_date": null,
    "business_value": 28000,
    "currency": "USD"
  },
  "version": 1
}
```

---

### `project.updated`

**Emitted by** `backend/apps/portfolio/services/update_project.py` on any field update that is not a workflow
transition, and by `backend/apps/portfolio/services/assign_project_workflow.py` when a project is
put on a lifecycle of its own or handed back to its engagement type's (`API.md` §2.20), as
`changes: {"workflow": {"from": "<code>", "to": "<code>"}}`. `workflow_state` can never appear in
`changes` — state moves only through `project.state_changed` (`CLAUDE.md` rule 2), and a
reassignment does not move the project: it keeps the state `code` it was standing on, so there is
no before-and-after to name. Version 1.

| Field | Type | Required |
|---|---|---|
| `changes` | object mapping field name → `{"from": any, "to": any}` | yes, non-empty |

**Consumed by** `priority-recalculator`, `snapshot-builder`. **SSE**: yes.

```json
{
  "id": "b0a3c9f5-9e2d-4f21-8a5b-1c4d7e9a0f33",
  "topic": "project.updated",
  "occurred_at": "2026-07-28T08:20:41Z",
  "actor": "daniel.rojas",
  "correlation_id": "b0a3c9f5-9e2d-4f21-8a5b-1c4d7e9a0f33",
  "entity": {"type": "project", "id": "PRJ-01"},
  "payload": {
    "changes": {
      "next_step": {"from": null, "to": "Unblock legal review with Atlas Foods"},
      "target_date": {"from": null, "to": "2026-08-14"}
    }
  },
  "version": 1
}
```

---

### `project.state_changed`

**Emitted by** `backend/apps/portfolio/services/transition_project.py`, only after
`backend/apps/workflow/services/transition.py` has
matched an active `WorkflowTransition` and satisfied its guard, `requires_reason` and
`requires_fields`. An illegal move raises `TransitionNotAllowed` and emits nothing. Version 1.

| Field | Type | Required |
|---|---|---|
| `from` | string (`WorkflowState.code`) | yes |
| `to` | string (`WorkflowState.code`) | yes |
| `from_category` | string (`BACKLOG \| IN_PROGRESS \| BLOCKED \| DONE \| CANCELLED`) | yes |
| `to_category` | same set | yes |
| `transition` | string (`WorkflowTransition` code) | yes |
| `reason` | string \| null | yes (non-null when the transition sets `requires_reason`) |

Consumers branch on `to_category`, never on `to` — that is what lets an operator add a state
from the admin without a deploy.

**Consumed by** `priority-recalculator`, `snapshot-builder`. **SSE**: yes.

```json
{
  "id": "1e7c4b90-2a55-4c0e-b0d2-8f4b1c66a201",
  "topic": "project.state_changed",
  "occurred_at": "2026-07-28T09:05:12Z",
  "actor": "daniel.rojas",
  "correlation_id": "1e7c4b90-2a55-4c0e-b0d2-8f4b1c66a201",
  "entity": {"type": "project", "id": "PRJ-01"},
  "payload": {
    "from": "execution",
    "to": "blocked",
    "from_category": "IN_PROGRESS",
    "to_category": "BLOCKED",
    "transition": "execution__blocked",
    "reason": "Atlas Foods legal has not returned the signed annex"
  },
  "version": 1
}
```

---

### `project.priority.recalculated`

**Emitted by** the `priority-recalculator` handler, not by an API route, and only when
the value or the breakdown actually changed — a recompute that lands on the same number emits
nothing. `actor` is `system`; `correlation_id` is inherited from the event that triggered the
recompute, so the timeline shows the state change and the reranking as one decision. Version 1.

| Field | Type | Required |
|---|---|---|
| `value` | number 0–100 | yes |
| `previous_value` | number 0–100 \| null | yes (null on first computation) |
| `policy_version` | string (`PriorityPolicy.version`) | yes — a label such as `v1`, not a number; it is copied verbatim from the row so a score stays readable after its policy is retired |
| `origin` | string (`POLICY \| MANUAL`) | yes |
| `breakdown` | array of `{code, label, raw, weight, contribution, reason}` | yes |
| `modifiers` | object mapping modifier code → number | no |
| `flags` | array of string flag codes | no |

`code` values in `breakdown` are the registered signal codes: `deadline_pressure`,
`overdue_work`, `criticality`, `business_value`, `blockage`, `staleness`. `label` and `reason` come
written in the interface's language, so a consumer renders a line it has never seen without mapping
its `code` to text of its own — which is what keeps a seventh signal a backend-only change.

**Consumed by** `snapshot-builder`. **Not** consumed by `priority-recalculator` — a handler
never consumes what it emits, which is what keeps the bus acyclic.
**SSE**: yes — the queue reorders live and the breakdown is what the UI shows next to the number.

```json
{
  "id": "9d21f4aa-77b1-4a2e-93cf-0c7b6d5e4411",
  "topic": "project.priority.recalculated",
  "occurred_at": "2026-07-28T09:05:13Z",
  "actor": "system",
  "correlation_id": "1e7c4b90-2a55-4c0e-b0d2-8f4b1c66a201",
  "entity": {"type": "project", "id": "PRJ-01"},
  "payload": {
    "value": 87.4,
    "previous_value": 71.2,
    "policy_version": "v2",
    "origin": "POLICY",
    "breakdown": [
      {"code": "deadline_pressure", "label": "Presión de fecha", "raw": 0.5, "weight": 0.25, "contribution": 12.5,
       "reason": "Sin fecha comprometida, la presión de fecha no se puede evaluar."},
      {"code": "overdue_work", "label": "Trabajo vencido", "raw": 0.5, "weight": 0.20, "contribution": 10.0,
       "reason": "2 de 4 tareas abiertas pasaron su fecha."},
      {"code": "criticality", "label": "Criticidad", "raw": 0.75, "weight": 0.15, "contribution": 11.25,
       "reason": "3 tarea(s) abierta(s) de prioridad urgente."},
      {"code": "business_value", "label": "Valor de negocio", "raw": 0.82, "weight": 0.15, "contribution": 12.3,
       "reason": "Valor de contrato 28.000,00 USD, normalizado logarítmicamente contra el máximo del portafolio (50.000,00)."},
      {"code": "blockage", "label": "Bloqueo", "raw": 1.0, "weight": 0.15, "contribution": 15.0,
       "reason": "1 bloqueo(s) abierto(s); el más antiguo lleva 0 día(s) sin resolverse y necesita intervención."},
      {"code": "staleness", "label": "Inactividad", "raw": 0.6, "weight": 0.10, "contribution": 6.0,
       "reason": "Sin actividad registrada durante 8 día(s) frente a un umbral de 14 día(s); no hay próximo paso registrado."}
    ],
    "modifiers": {"engagement_type": 1.1},
    "flags": ["NO_TARGET_DATE", "OWNER_OVERLOADED"]
  },
  "version": 1
}
```

---

### ~~`project.risk.changed`~~ — retired (ADR 0011)

**This topic does not exist.** Risk flags are computed on read, so there is no moment at which a
flag "changes": no previous set to diff against, no event to emit and no handler to emit it. The
flags a client used to receive here now travel in **every project payload** —
`GET /api/v1/queue` and `GET /api/v1/projects/{code}` both return `risk_flags` and `health`,
evaluated at the instant of the request (`docs/API.md` §1.6).

Flag codes still come from the risk registry: `BLOCKED`, `OVERDUE`, `NO_NEXT_STEP`,
`NO_TARGET_DATE`, `STALE`, `OWNER_OVERLOADED`. Severity ∈ `LOW | MEDIUM | HIGH | CRITICAL`, taken
from the registry entry. `CRITICAL` is what makes health `BLOCKED`; any other raised flag makes it
`AT_RISK`. Health remains the one derived vocabulary in the system and it is **English**, matching
`prioritization.domain.types.Health` — Spanish belongs to operator-editable *labels*, which never
appear in a payload.

**What a live client does instead.** A project's flags can only change because something about it
changed, and everything that can change about it is already a topic. `project.state_changed`,
`task.state_changed`, `blocker.raised` and `blocker.resolved` all reach the browser, and the
project they name is re-read with its freshly evaluated flags. The one case with no event behind it
is the calendar: a project that becomes overdue or stale at midnight pushes nothing, and the board
shows it the moment anyone loads or re-reads it. That trade is recorded in ADR 0011.

---

### `task.created`

**Emitted by** `backend/apps/work/services/create_task.py`. `entity` is the task; `payload.project_code` is
what lets a consumer aggregate without a FK into `backend/apps/work`. Version 1.

| Field | Type | Required |
|---|---|---|
| `project_code` | string | yes |
| `title` | string | yes |
| `priority` | string (`Priority.code`) | yes |
| `state` | string (initial `WorkflowState.code`) | yes |
| `assignee_alias` | string \| null | yes |
| `due_date` | string `YYYY-MM-DD` \| null | yes |
| `depends_on` | array of `{task_code \| null, raw_label}` | no (unresolved dependencies keep only `raw_label`) |

**Consumed by** `priority-recalculator`, `snapshot-builder`. **SSE**: yes — task
counts and owner load are on screen.

```json
{
  "id": "a51d0e33-4c2b-49a8-8e6a-77f0d1c9b420",
  "topic": "task.created",
  "occurred_at": "2026-07-28T09:31:00Z",
  "actor": "daniel.rojas",
  "correlation_id": "a51d0e33-4c2b-49a8-8e6a-77f0d1c9b420",
  "entity": {"type": "task", "id": "PRJ-01-T02"},
  "payload": {
    "project_code": "PRJ-01",
    "title": "Resolve priority issue in pilot or production - Global Contract Management",
    "priority": "critica",
    "state": "todo",
    "assignee_alias": "Daniel Rojas",
    "due_date": "2026-07-10",
    "depends_on": [
      {"task_code": "PRJ-01-T04", "raw_label": "Functional validation and release checklist"}
    ]
  },
  "version": 1
}
```

---

### `task.updated`

**Emitted by** `backend/apps/work/services/update_task.py` on any field edit that is not a workflow
transition, and by `backend/apps/work/services/assign_task_workflow.py` when a task is put on a
lifecycle of its own or handed back to the inherited one (`API.md` §2.20), as
`changes: {"workflow": {"from": "<code>", "to": "<code>"}}`. `workflow_state` can never appear in
`changes` — state moves only through `task.state_changed` (`CLAUDE.md` rule 2), and a reassignment
keeps the state `code` the task was standing on. An edit that changes nothing emits nothing, exactly
as `project.updated`: a no-op event teaches consumers to recompute for nothing. Version 1.

| Field | Type | Required |
|---|---|---|
| `project_code` | string | yes |
| `changes` | object mapping field name → `{"from": any, "to": any}` | yes, non-empty |

Keys of `changes` are model field names (`title`, `detail`, `last_progress`, `due_date`,
`priority`, `assignee`, `workflow`). Values are rendered the same way as in `project.updated`: a foreign key
as the referenced row's business `code`, a `Decimal` as a number, a `date` as an ISO string, and
`null` when the field is empty — `null` and `""` are different facts.

`depends_on` is the one key that is not a column: it names the task's prerequisite set, which
`PATCH /api/v1/tasks/{code}` replaces whole, and its `from`/`to` are therefore **arrays of strings**
— the prerequisite's task `code` when the edge resolved, the operation's own words when it did not.
A set-valued field's honest before/after is the set; flattening it to a count or to a joined line
would make "did this task stop waiting on PRJ-01-T02" unanswerable without re-reading the graph.

**Consumed by** `priority-recalculator`, `snapshot-builder`. **SSE**: yes — a
priority or due-date edit reorders the queue, and without this topic the score goes stale
silently: neither is a transition, and `clock.ticked` does not rescue it because
`PriorityScore.valid_until` only moves when something recomputes the score.

```json
{
  "id": "3fb27c48-0d61-4f9e-b6a3-51c0d8e7a914",
  "topic": "task.updated",
  "occurred_at": "2026-07-28T09:41:07Z",
  "actor": "daniel.rojas",
  "correlation_id": "3fb27c48-0d61-4f9e-b6a3-51c0d8e7a914",
  "entity": {"type": "task", "id": "PRJ-01-T02"},
  "payload": {
    "project_code": "PRJ-01",
    "changes": {
      "priority": {"from": "alta", "to": "critica"},
      "due_date": {"from": "2026-07-10", "to": "2026-08-03"},
      "assignee": {"from": "daniel.rojas", "to": "camila.torres"}
    }
  },
  "version": 1
}
```

---

### `task.state_changed`

**Emitted by** `backend/apps/work/services/transition_task.py` for a task aggregate, under the same
`backend/apps/workflow/services/transition.py` validation as a project. Version 1.

| Field | Type | Required |
|---|---|---|
| `project_code` | string | yes |
| `from` | string (`WorkflowState.code`) | yes |
| `to` | string (`WorkflowState.code`) | yes |
| `from_category` | string category | yes |
| `to_category` | string category | yes |
| `transition` | string | yes |
| `reason` | string \| null | yes |
| `last_progress` | string \| null | no |

**Consumed by** `priority-recalculator`, `snapshot-builder`. **SSE**: yes — a
task entering `BLOCKED` can change the whole project's health, which is visible on the queue.

```json
{
  "id": "c7d94b02-1f6e-4a3d-9b8c-5e2a7f3d1108",
  "topic": "task.state_changed",
  "occurred_at": "2026-07-28T09:47:22Z",
  "actor": "camila.torres",
  "correlation_id": "c7d94b02-1f6e-4a3d-9b8c-5e2a7f3d1108",
  "entity": {"type": "task", "id": "PRJ-01-T02"},
  "payload": {
    "project_code": "PRJ-01",
    "from": "todo",
    "to": "in_progress",
    "from_category": "BACKLOG",
    "to_category": "IN_PROGRESS",
    "transition": "todo__in_progress",
    "reason": null,
    "last_progress": "Reproduced the issue on the pilot tenant"
  },
  "version": 1
}
```

---

### `task.archive_changed`

**Emitted by** `backend/apps/work/services/update_task.py` when a task is removed from the
operation's attention (`DELETE /api/v1/tasks/{code}`) or put back
(`PATCH {"is_archived": false}`). Version 1.

**One topic for both directions**, carrying `is_archived`, rather than a `task.deleted` and a
`task.restored`. A subscriber's question is a boolean — "does this task still count?" — and
answering it with a topic name would force every subscription to list both and handle them
identically, which is the shape of the bug the first time somebody adds only one of them.
`member.activation_changed` is the same decision for the same reason.

Removal is a **soft delete** ([ADR 0012](adr/0012-soft-delete-for-tasks.md)). The row, its
dependency edges, its notes and its blockers all survive, so a consumer must never read
`is_archived: true` as "this code no longer exists": the code is never reused, and the task can
come back on this same topic.

| Field | Type | Required |
|---|---|---|
| `project_code` | string | yes |
| `title` | string | yes |
| `is_archived` | boolean | yes |

`project_code` travels so a consumer aggregates without a foreign key into `work`: the counts that
move when a task is removed — open, overdue, urgent, blocked — are read per project.

**Emitted only when the flag actually moves.** Removing an already-removed task writes nothing, so
this topic never carries a non-change and a redelivery is the only way to see it twice.

**Consumed by** `priority-recalculator`, `snapshot-builder`. **SSE**: yes — a removed task has to
leave the board and the project's counts without a refresh.

```json
{
  "id": "1f5a3c88-4b21-4d6e-8a70-9c2e5b7f0031",
  "topic": "task.archive_changed",
  "occurred_at": "2026-07-29T11:04:07Z",
  "actor": "camila.torres",
  "correlation_id": "1f5a3c88-4b21-4d6e-8a70-9c2e5b7f0031",
  "entity": {"type": "task", "id": "PRJ-01-T02"},
  "payload": {
    "project_code": "PRJ-01",
    "title": "Validar checklist de release con Legal",
    "is_archived": true
  },
  "version": 1
}
```

---

### `blocker.raised`

**Emitted by** `backend/apps/work/services/raise_blocker.py`. A blocker is a first-class row attached to a
project or a task, never a substring in a notes field. `entity` is the blocker; the payload
names what it blocks. Version 1.

| Field | Type | Required |
|---|---|---|
| `project_code` | string | yes (for a task blocker, the task's project) |
| `task_code` | string \| null | yes (null when the blocker is attached to the project) |
| `kind` | string (`EXTERNAL_DEPENDENCY \| ACCESS \| DECISION \| TECHNICAL`) | yes |
| `description` | string | yes |
| `owner_alias` | string \| null | yes — who has to move it |
| `raised_at` | string ISO-8601 | yes |

**Consumed by** `priority-recalculator`, `snapshot-builder`. **SSE**: yes — the
open-blockers panel is the second question the command center answers.

```json
{
  "id": "e2b6f0d7-3a41-4c85-9d2f-6b1e8c4a7d55",
  "topic": "blocker.raised",
  "occurred_at": "2026-07-28T09:06:02Z",
  "actor": "daniel.rojas",
  "correlation_id": "1e7c4b90-2a55-4c0e-b0d2-8f4b1c66a201",
  "entity": {"type": "blocker", "id": "BLK-0142"},
  "payload": {
    "project_code": "PRJ-01",
    "task_code": "PRJ-01-T03",
    "kind": "EXTERNAL_DEPENDENCY",
    "description": "Waiting on client response, credentials, external API or business definition.",
    "owner_alias": "Daniel Rojas",
    "raised_at": "2026-07-28T09:06:02Z"
  },
  "version": 1
}
```

---

### `blocker.resolved`

**Emitted by** `backend/apps/work/services/resolve_blocker.py` when `resolved_at` is set. Resolving an already
resolved blocker is a no-op and emits nothing. Version 1.

| Field | Type | Required |
|---|---|---|
| `project_code` | string | yes |
| `task_code` | string \| null | yes |
| `kind` | string | yes |
| `resolution` | string | yes — how it was unblocked, mandatory |
| `resolved_at` | string ISO-8601 | yes |
| `open_for_days` | integer | yes — age at resolution; the blockage signal reads it |

**Consumed by** `priority-recalculator`, `snapshot-builder`. **SSE**: yes.

```json
{
  "id": "07f3a8c1-9b24-4de6-a0f7-2c5d9e1b3a86",
  "topic": "blocker.resolved",
  "occurred_at": "2026-07-30T14:22:10Z",
  "actor": "camila.torres",
  "correlation_id": "07f3a8c1-9b24-4de6-a0f7-2c5d9e1b3a86",
  "entity": {"type": "blocker", "id": "BLK-0142"},
  "payload": {
    "project_code": "PRJ-01",
    "task_code": "PRJ-01-T03",
    "kind": "EXTERNAL_DEPENDENCY",
    "resolution": "Atlas Foods returned the signed annex; sandbox credentials received",
    "resolved_at": "2026-07-30T14:22:10Z",
    "open_for_days": 2
  },
  "version": 1
}
```

---

### `note.added`

**Emitted by** `backend/apps/work/services/add_note.py`. A note is activity, so it resets the staleness
signal — which is why the engine handlers consume it even though a note changes no field.
Version 1.

| Field | Type | Required |
|---|---|---|
| `project_code` | string | yes |
| `task_code` | string \| null | yes |
| `body` | string | yes |
| `author_alias` | string | yes |

**Consumed by** `priority-recalculator`, `snapshot-builder`. **SSE**: yes — the
detail timeline appends live.

```json
{
  "id": "5a9c1e70-8d3f-42b6-9c14-4e7b0a2f6d19",
  "topic": "note.added",
  "occurred_at": "2026-07-28T10:14:35Z",
  "actor": "camila.torres",
  "correlation_id": "5a9c1e70-8d3f-42b6-9c14-4e7b0a2f6d19",
  "entity": {"type": "note", "id": "NOTE-0391"},
  "payload": {
    "project_code": "PRJ-01",
    "task_code": "PRJ-01-T02",
    "body": "Escalated to the Atlas Foods legal contact; answer expected Thursday.",
    "author_alias": "Camila Torres"
  },
  "version": 1
}
```

### `clock.ticked`

The only topic that is not caused by a person. It exists because two prioritization signals —
`deadline_pressure` and `staleness` — are functions of *now*, not of any mutation. A project
crosses its target date, or goes stale, without anybody touching it, and a purely
change-driven system never notices. The clock therefore has to be an explicit participant
rather than an assumption.

**Emitted by** two Celery Beat tasks: `events.emit_interval_tick`, scheduled every
`TICKER_INTERVAL_SECONDS` (default 300), and `events.emit_day_boundary_tick`, scheduled at
`crontab(hour=0, minute=0)`. Beat schedules them and the single `worker` service runs them, the
same worker that drains the outbox and applies every handler. Both tasks write
to the outbox like every other producer; they do not publish to Redis directly and they name no
handler. The clock is dispatched by the same drain as a transition.

**Payload**

| Field | Type | Required | Meaning |
|---|---|---|---|
| `tick_at` | ISO-8601 | yes | The instant the tick represents. Consumers use this, never their own wall clock, so a replayed tick is deterministic. |
| `kind` | `"interval"` \| `"day_boundary"` | yes | A day boundary is when the calendar-derived signals (`deadline_pressure`, `staleness`) are most likely to have moved a stored score. |

`entity` is `{"type": "clock", "id": "system"}`. There is no project in scope: the handler
decides which projects a tick affects.

**Consumed by** `priority-recalculator`. **SSE**: no. A tick itself is not a fact a view
renders; the `project.priority.recalculated` events it produces are, and those reach the browser
normally. A tick no longer re-flags anything: a project becomes overdue at midnight whether or not
anything ticked, because the flag is evaluated when somebody reads the project. What the tick still
buys is the *score*, which is stored and therefore has to be told that the calendar moved.

**What the handler does with it.** Recomputing all 22 projects on every tick would work at this
size and would be the wrong shape at any other. Each `PriorityScore` persists `valid_until`: the
earliest future instant at which a time-dependent signal changes bucket — the target date, the
start of the final week, or the staleness threshold, whichever comes first. On a tick the
handler selects only `WHERE valid_until <= tick_at` and recomputes those. A quiet tick costs one
index scan and emits nothing.

```json
{
  "id": "0f2c9e5a-8d41-4a77-9c0e-7b2f3a51d6c4",
  "topic": "clock.ticked",
  "occurred_at": "2026-07-29T00:00:00Z",
  "actor": "system",
  "correlation_id": "0f2c9e5a-8d41-4a77-9c0e-7b2f3a51d6c4",
  "entity": {"type": "clock", "id": "system"},
  "payload": {"tick_at": "2026-07-29T00:00:00Z", "kind": "day_boundary"},
  "version": 1
}
```

The cost accepted: a score can be at most one tick stale with respect to time. Data changes,
which are the ones a human just made and is watching for, still propagate immediately.

---

### `member.created`, `member.updated`, `member.activation_changed`

The roster's three topics. **Emitted by** `backend/apps/accounts/services/` — `create_member.py`
and `update_member.py` — from the `/api/v1/team/members` routes. Version 1.
`entity` is `{"type": "member", "id": "<User.code>"}`.

Retiring and restoring share **one** topic carrying `is_active`, rather than being two. A
subscriber's question is "may this person still take work?", and the answer is a field; two topics
would make every subscription list both and treat them identically, which is the shape of the bug
where somebody later adds only one of them.

| Topic | Payload | Notes |
|---|---|---|
| `member.created` | `{label, role: str \| null, weekly_capacity_points, is_active}` | The whole person, so a subscriber renders the new row without a follow-up read. No load: it is computed from task rows at read time and a new person has none, so a number here could only be a zero that goes stale immediately. |
| `member.updated` | `{label, role, weekly_capacity_points, changed: str[]}` | `changed` names which wire fields moved — `label`, `role`, `weekly_capacity_points` — so a consumer deciding whether to act need not diff against a copy it does not have. Never empty: a service that found nothing to change emits nothing at all. |
| `member.activation_changed` | `{label, is_active}` | The person's existing work is untouched. `is_active: false` retires them from *new* assignment and nothing reassigns what they already hold, so a consumer must not read this as "their tasks are now unowned". |

**Consumed by** `sse-fanout` only. **SSE**: yes — the Equipo surface refetches on any of them, so
an edit made in one tab appears in another.

**Not consumed by `priority-recalculator`**, and that is deliberate. Raising somebody's capacity
changes whether they are overloaded, and being overloaded is a `OWNER_OVERLOADED` *flag* — computed
on read from the current task rows (ADR 0011), never stored — so there is nothing for a
recomputation to persist. Subscribing anyway would rescore every project that person owns on every
roster edit, to arrive at the same number.

**There is no `member.password_reset` topic**, on purpose. An event is broadcast to every
subscriber and replayed from a durable row months later; "somebody's password was replaced" is of
interest to no consumer, and putting it on a channel the browser reads would leak the timing of
credential changes to every open tab for nothing. The reset is recorded in the audit trail — actor,
subject and instant, never the value — which is where the question is actually asked.

**Role changes emit nothing at all.** `POST`/`PATCH /api/v1/catalog/roles` write an
`ActivityRecord` under `entity_type: "role"` and no event: a role is picker vocabulary, nothing
recomputes from it, no read model denormalizes it, and the surfaces that render one refetch the
catalog.

## 5. Handlers

Four registered handlers. **One handler per reason to react**: a handler that fails does not stop
the others, because each event is dispatched as its own `events.handle_event` task per handler, and
no handler is ever shared across concerns.

A handler is a function in `apps/<context>/handlers.py` decorated with `@register_handler`. The
name in the table below is the registry name — the first argument of the decorator, the
`handler_name` of the delivery task, and the `handler` column of `ProcessedEvent`. It is released
API: renaming a deployed handler replays history for it.

| Handler (registry name) | Declared in | Subscribes to | What it does | Emits | Idempotency key |
|---|---|---|---|---|---|
| `priority-recalculator` | `apps/prioritization/handlers.py` | `project.created`, `project.updated`, `project.state_changed`, `task.created`, `task.updated`, `task.state_changed`, `task.archive_changed`, `blocker.raised`, `blocker.resolved`, `note.added`, `clock.ticked` | Recomputes `PriorityScore` for the affected project under the active `PriorityPolicy`, persisting `value`, `policy_version` and `breakdown` | `project.priority.recalculated`, only when value or breakdown changed | `(event.id, "priority-recalculator")` |
| `snapshot-builder` | `apps/portfolio/handlers.py` | every topic except `clock.ticked` (§8 read model) | Rebuilds the `ProjectSnapshot` row for the project named by `entity.id` or `payload.project_code`: score, owner load, task and blocker counts. **Facts only** — flags and health are derived when the queue is read | nothing | `(event.id, "snapshot-builder")` |
| `sse-fanout` | `apps/events/handlers.py` | every topic on the allowlist | `PUBLISH aztec.sse` with the envelope unchanged, for `GET /api/stream` to frame | nothing | `(event.id, "sse-fanout")` |

Notes:

- **The producer never appears in this table.** A service writes an outbox row naming a topic; the
  drain asks `handlers_for(topic)` who cares. Adding a reactor is one decorated function and zero
  edits anywhere upstream — `CLAUDE.md` rule 8, applied to the bus.
- **The module name is fixed.** App-ready calls `autodiscover_modules("handlers")`, so a reactor
  declared in `apps/<context>/reactors.py` is never imported and silently never runs. There is no
  error; the event is simply dispatched to nobody.
- The idempotency key is a `ProcessedEvent` row with a unique constraint on
  `(event_id, handler)`. The same event is legitimately processed once **per handler**.
- `priority-recalculator` does not consume the topic it emits. The derived topic feeds only
  `snapshot-builder` and `sse-fanout`, which emit nothing — the graph has no cycle by construction.
- **There are three handlers where there were four.** `risk-evaluator` is gone with the table it
  wrote (ADR 0011). A derived value computed on read has no previous value to diff against, so it
  has no change event and needs no reactor; the flags travel in every project payload instead. The
  cost is stated in the ADR: the board no longer pushes "this became at risk" on its own.
- `snapshot-builder` derives its subscription by subtraction (`ALL_TOPICS - {clock.ticked}`) rather
  than by a list, because the failure mode of forgetting a new topic is a silently stale command
  center. What a tick *causes* arrives here as its own event, so nothing is missed.
- Every topic except `clock.ticked` is on the `sse-fanout` allowlist, because each of those ten
  changes something a view renders. `clock.ticked` is the standing example of the opposite case: it
  triggers recomputation and is never forwarded, since a browser has its own clock. The allowlist
  still exists and defaults to off: a topic that only triggers internal recomputation must not be
  forwarded, and a topic added to the fanout without being added to
  `frontend/src/lib/stream/topics.ts` is published, received and silently discarded.
- `sse-fanout` is the only handler permitted to hold a Redis client. Publishing *is* its effect,
  so there is no database write for it to be inconsistent with; anywhere else a `PUBLISH` beside a
  write would be the dual write the outbox exists to prevent.
- Adding a handler means adding a row here. A handler that is not in this table is not deployed.

## 6. Delivery guarantees

**At-least-once, never at-most-once.** The outbox row and the state change share one transaction,
so an event is never lost. The drain can die after queuing the delivery tasks and before its own
commit, or a worker can be killed after applying a handler and before acking, so an event may be
delivered twice. Every handler must produce the same result when run twice with the same
`event.id`.

**Deduplication.** `apply_once` opens a transaction, inserts `ProcessedEvent(event_id, handler)`,
and calls the handler in that same transaction. A duplicate hits the unique constraint, the task
returns `duplicate` without reprocessing, and the message is acked — a duplicate that raised would
retry forever. There is no `SELECT` before the insert: check-then-insert races between two workers
holding the same delivery, whereas the unique constraint decides atomically. Because the insert
shares the handler's transaction, a failed attempt leaves no `ProcessedEvent` row, so a retry is a
real retry and not a silent skip.

**Ack after the task returns.** `CELERY_TASK_ACKS_LATE = True`, with
`CELERY_TASK_REJECT_ON_WORKER_LOST = True`. This is load-bearing rather than a default: the drain
marks the outbox row published *before* the handler runs, so an early ack plus a worker kill would
drop an event the outbox already believes was delivered.

**Retry with backoff.** An unexpected exception propagates out of the handler. `events.handle_event`
retries with exponential backoff and jitter (roughly 1s, 2s, 4s, 8s, 16s) up to
`EVENT_MAX_ATTEMPTS` (default 5). Nothing is swallowed: every attempt logs `topic`, `event.id`,
`handler` and `attempt`, and the exception is re-raised so the Celery result is a failure too. A
`try/except: pass` anywhere in a handler is a bug — it is an event that vanished.

**Dead letter.** After the retry budget the *outbox row itself* is dead-lettered:
`dead_lettered_at`, `last_error` and `attempts` are set on the row that already exists. Nothing is
copied to a second queue and nothing is deleted, so the failed event is still the row carrying its
topic, payload and correlation id. It is visible in the Django admin under the
**pending / dispatched / dead-lettered** filter, and **"Re-queue selected dead-lettered events"** is
the admin action that replays it: `dead_lettered_at` is cleared and `published_at` reset, so the
next drain dispatches the same envelope with the same `event.id`. Handlers that already applied it
dedup on `ProcessedEvent`; the handler that failed applies it for the first time. An event dying
silently is worse than a loud error.

**What we gave up, and the replacement.** There is no `XPENDING`, no per-group lag figure and no
stream replay. The outbox table is the instrument instead — see [ADR 0010](adr/0010-celery-as-the-bus.md).
the outbox admin (`/admin/events/outboxevent/`) prints pending / dispatched / dead-lettered counts, `GET /api/v1/health/pipeline`
serves the same numbers plus the oldest unpublished age and the last tick, and the whole history is
one `SELECT` away in PostgreSQL rather than behind a purpose-built inspection command.

**Ordering.** The drain claims outbox rows in insertion order, but delivery is one independent task
per handler per event, so **nothing guarantees ordering across handlers or even across events under
concurrency**. `sse-fanout` can reach the browser before `snapshot-builder` has rebuilt the read
model. Islands therefore patch from the event payload and tolerate a `ProjectSnapshot` that is one
event behind on a refetch. Handlers that care compare `occurred_at` against the state they hold
rather than assuming they are seeing the newest fact — which is also why a handler takes its clock
from `envelope.occurred_at` (or `payload.tick_at`) and never from `timezone.now()`.

## 7. Checklist — adding a topic

1. Name it under §3 and add a full entry to §4 in the same change: emitter, payload table with
   types and required flags, handlers, SSE, example payload using real dataset values.
   An undocumented topic does not exist — refuse to publish one.
2. Add the line to the topic list in `docs/ARCHITECTURE.md` §6 and point it here.
3. Register the topic constant in `backend/apps/events/domain/envelope.py`. The registry validates
   every subscription against that catalog at import, so an unregistered topic is a boot failure
   rather than a handler that never fires.
4. Declare the payload as a `pydantic.BaseModel` in `backend/apps/<context>/domain/events.py` — pure,
   no Django import. `version` starts at 1.
5. Emit it from `backend/apps/<context>/services/`, inside the same `transaction.atomic()` as the
   aggregate mutation and the `ActivityRecord`, reusing the request's `correlation_id`. If a
   module under `services/` imports `redis` or calls `.delay()`, stop and fix that instead.
6. Put the business code in `entity.id`, and `project_code` in the payload for any non-project
   entity, so no handler needs a FK into another context.
7. Decide which handlers consume it and add it to their subscription column in §5 — which means
   adding the constant to the `topics=` set of a `@register_handler` in that context's
   `handlers.py`, never editing a producer. Confirm it reaches `snapshot-builder` if it changes
   anything the command center renders.
8. Decide whether it reaches the browser. If yes, add it to the `sse-fanout` allowlist **and** to
   `frontend/src/lib/stream/topics.ts`, and subscribe an island to it. If no, leave it out rather than
   publishing noise the client discards.
9. Write the tests required by `docs/ARCHITECTURE.md` §11, on `TransactionTestCase` for anything
   touching the outbox or `on_commit`: the service call writes exactly one `OutboxEvent` with the
   right topic and version; a rolled-back transaction writes zero; the drain dispatches one task
   per subscribed handler; the handler applied twice with the same `event.id` produces one effect
   and reports `duplicate` the second time. Narrow the registry with `only_handlers(...)` from
   `apps/events/tests/registry_support.py` so a committed row does not fan out to `sse-fanout` and
   reach Redis.
10. `make test` and `make lint`. When an event does not arrive, the outbox admin (`/admin/events/outboxevent/`) and
    `/api/v1/health/pipeline` say whether it was ever dispatched; `make logs s=worker` says what the
    handler did with it.
