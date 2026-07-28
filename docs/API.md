# HTTP API — Aztec Ops

Contract between the Django/django-ninja backend and the Astro frontend. `docs/ARCHITECTURE.md`
is normative for behaviour; this file is normative for the wire. If a route here and a router in
`backend/apps/*/api/` disagree, one of the two gets fixed.

Schemas are written as Pydantic-shaped field lists because that is what `backend/apps/*/api/schemas.py`
declares. Every route has typed input and output schemas — no bare `dict` in any `response=`.
Examples use real values from `data/raw/dataset.json`, evaluated at `now = 2026-07-28`.

## 1. Conventions

### 1.1 Base path and versioning

| Surface | Path | Notes |
|---|---|---|
| REST API | `/api/v1/...` | Mounted on a single `NinjaAPI(version="1.0.0", urls_namespace="api-v1")` in `config/api.py`. |
| OpenAPI schema | `/api/v1/openapi.json` | Source for `frontend/src/lib/api/types.ts` via `npx openapi-typescript`. Never hand-transcribe types. |
| Event stream | `/api/stream` | Unversioned on purpose: the browser keeps it open across deploys, and the envelope carries its own `version` field. |

Breaking a response shape means `/api/v2`, not an in-place edit. Adding an optional field to an
`*Out` schema is not breaking.

### 1.2 Authentication

Every route is authenticated. **Authentication is the default of the `NinjaAPI` instance, not a
per-route decoration**, so an endpoint added tomorrow is protected because its author did nothing.
Five routes opt out, and that list is stated in a comment in `backend/config/api.py` so a sixth is
a visible decision:

| Route | Why it is open |
|---|---|
| `GET /api/v1/health/live` | A kubelet probe runs before anything can present a token. |
| `GET /api/v1/health/ready` | The load balancer's drain signal, same reason. |
| `GET /api/v1/health/pipeline` | Operational counters; no business data. |
| `POST /api/v1/auth/token` | Obtaining a token cannot require a token. |
| `POST /api/v1/auth/token/refresh` | Reachable precisely when the access token has expired. |

#### 1.2.1 Obtaining a token

```
POST /api/v1/auth/token
{"username": "camila.torres", "password": "..."}
```

```json
{
  "access": "eyJhbGciOiJIUzI1NiIs...",
  "refresh": "eyJhbGciOiJIUzI1NiIs...",
  "expires_in": 1800,
  "actor": {"alias": "camila.torres", "label": "Camila Torres", "role": "Delivery"},
  "is_ops_lead": true
}
```

`username` is the account's `username`, which mirrors `accounts.User.code`. `expires_in` is
seconds, not an instant, so a client with a skewed clock still schedules its refresh correctly.
`actor` and `is_ops_lead` are returned so the frontend never decodes the token itself — a client
that parses an unverified JWT payload is a client that trusts one.

Failures are `401 invalid_credentials` for **all** of unknown username, wrong password and
deactivated account. One answer for three causes, on purpose: distinguishing them turns the sign-in
form into an account-enumeration oracle.

```
POST /api/v1/auth/token/refresh   {"refresh": "..."}   -> 200 AccessGrant (no `refresh` field)
POST /api/v1/auth/logout                               -> 204, clears the cookie
```

Refresh returns the same shape *minus* `refresh`, so a client cannot accidentally overwrite the
refresh token it still holds with `null`. The account is re-read on every refresh rather than
trusted from the token's claims, which is what makes deactivating somebody take effect within one
access-token lifetime. `logout` is itself authenticated — an unauthenticated endpoint that deletes
a cookie is a cross-origin nuisance that logs people out — and it clears the cookie without
revoking the tokens: there is no blacklist (ARCHITECTURE §12).

#### 1.2.2 Header **and** cookie — and why both exist

The access token is delivered twice: in the response body, **and** as a cookie.

```
Authorization: Bearer eyJhbGciOiJIUzI1NiIs...        # curl, tests, fetch
Cookie: aztec_access=eyJhbGciOiJIUzI1NiIs...          # EventSource, automatically
```

**`EventSource` cannot set request headers.** There is no way for a browser to send
`Authorization: Bearer` to `GET /api/stream` (§3), and the stream is the entire live surface of the
product. So the token is also set as a cookie, which the browser attaches when the client opens the
stream with `withCredentials: true`:

```js
new EventSource(`${API_BASE}/api/stream`, { withCredentials: true })
```

Cookie attributes: `HttpOnly` (no script can read it, so an XSS cannot walk away with a usable
credential), `Secure` outside local development (a browser discards a `Secure` cookie over the plain
HTTP the compose stack serves), `SameSite=Lax`, `Path=/api/`, and a `Max-Age` equal to the token's
own lifetime so a stale cookie expires with the credential inside it.

**The cookie is honoured on `GET`, `HEAD` and `OPTIONS` only.** django-ninja marks its views
CSRF-exempt, which is correct for a header-authenticated API and fatal for a cookie-authenticated
one: a form on any origin could POST to a transition route and the browser would attach the cookie.
Restricting the cookie to methods that cannot change state removes that class of attack
structurally rather than relying on `SameSite` alone, and it costs nothing — the cookie exists for
exactly one caller, and `EventSource` can only issue a `GET`. **Every write presents the header.**

**A token is never accepted in the query string.** It would land in the access log of every proxy
on the path, in the `Referer` of anything the page links to, and in browser history.

CORS: `CORS_ALLOW_CREDENTIALS = True` and an explicit origin allowlist. That is not a preference —
a browser refuses a credentialed cross-origin response whose `Access-Control-Allow-Origin` is `*`,
so the moment the token became a cookie the wildcard stopped being available at all.

#### 1.2.3 Permissions: two levels, and only two

Aztec Ops is a Jira for the operation, so it is **collaborative, not ownership-scoped**.

**Level 1 — authenticated.** Any member may act on *any* project or task: create, update,
transition, raise and resolve blockers, add notes, recompute a single project. There are no
object-level "only your own" rules, and there should not be: a colleague must be able to unblock a
project while its owner is on holiday. "My tickets" is a **filter** — `?assignee=camila.torres` on
the task list, `?owner=` on the queue — exactly as it is in Jira. A filter, never a restriction.

**Level 2 — ops lead.** Two capabilities, both of which overrule the engine rather than feed it:

| Route | Why it is gated |
|---|---|
| `POST /projects/{code}/priority-override` | Forces the ranking against the computed score. |
| `DELETE /projects/{code}/priority-override` | The other half of the same capability: a rank one person may force and anybody may lift is a suggestion, not a decision. |
| `POST /recompute` | Portfolio-wide and expensive. `POST /projects/{code}/recompute` is *not* gated — its blast radius is one project. |

An ops lead is `accounts.User.is_staff`. **Why that and not the `role` foreign key:** `catalog.Role`
is operator-editable taxonomy — rows are renamed, reordered and retired from the admin like every
other taxonomy — so hanging authorization off it would let a label edit silently revoke a
permission. Mapping it onto a Django `Group` would fix the fragility and cost a migration, a
permission codename, a fixture and a synchronisation rule, which is real machinery for two `if`s.
`is_staff` is already on `AbstractUser`, is identity rather than taxonomy, and is editable from the
admin. The overlap with admin access is deliberate: the person trusted to edit workflows and
priorities from `/admin/` is the person trusted to force a rank. The rule lives in one place,
`User.is_ops_lead`.

The token response's `is_ops_lead` is what the frontend renders the override control from. It
should still handle a `403`, because the answer of record is the API's.

#### 1.2.4 Authentication errors

| Situation | HTTP | `code` | What the client does |
|---|---|---|---|
| No credential presented | 401 | `authentication_required` | Show the sign-in form. |
| Token expired, malformed, wrong type, or its account cannot sign in | 401 | `invalid_token` | Try `POST /auth/token/refresh`; if that also fails, sign in. |
| Wrong username or password, or inactive account | 401 | `invalid_credentials` | Re-prompt. |
| Authenticated, but not an ops lead | 403 | `permission_denied` | Hide or disable the control. `details.required` is `"ops_lead"`, `details.action` names what was refused. |

All four use the §1.5 envelope, including on `GET /api/stream`, which is a plain Django view and
renders its refusal through the same table so the client has one parser.

`ActivityRecord.actor` and every event envelope's `actor` are written from `request.user.code`. They
are now facts rather than claims, which is the entire point of this section.

### 1.3 Pagination

All list endpoints are paginated with ninja's `PageNumberPagination`. Query params:

| Param | Type | Default | Notes |
|---|---|---|---|
| `page` | int ≥ 1 | 1 | |
| `page_size` | int 1–200 | 50 | Values above the cap are clamped, not rejected. |

Response envelope, always:

```json
{ "items": [ ... ], "count": 22 }
```

`count` is the total matching rows, not the page length. The frontend derives the page count;
the API does not send `next`/`previous` URLs.

### 1.4 Filtering and ordering

Filters are declared as a typed `FilterSchema` bound with `Query(...)`. Nothing is read from
`request.GET`. Unknown query params are ignored; a known param with an unparseable value is
`422 validation_error`.

Shared filters on project-list surfaces:

| Param | Type | Meaning |
|---|---|---|
| `client` | str | `Client.alias` slug. Repeatable → OR. |
| `owner` | str | `accounts.User.code` slug. Repeatable → OR. |
| `engagement_type` | str | `EngagementType.code`. Repeatable. |
| `project_type` | str | `ProjectType.code`. |
| `stage` | str | `Stage.code`. |
| `state` | str | `WorkflowState.code`. |
| `state_category` | enum | `BACKLOG \| IN_PROGRESS \| BLOCKED \| DONE \| CANCELLED`. |
| `risk_flag` | str | A `RiskFlag.code` (`BLOCKED`, `OVERDUE`, `NO_NEXT_STEP`, `NO_TARGET_DATE`, `STALE`, `OWNER_OVERLOADED`). Repeatable → AND. |
| `health` | enum | `HEALTHY \| AT_RISK \| BLOCKED`. |

`risk_flag` and `health` are the two **derived** facets. Neither is a stored column: the flags are
evaluated per request from the rows the read already loaded ([ADR 0011](adr/0011-risk-flags-computed-on-read.md)),
so filtering on them is applied after evaluation rather than by the database. It behaves exactly as
before — repeatable `risk_flag` still ANDs — and it is the one filter combination that does not use
an index. An unknown flag code matches nothing rather than erroring.
| `has_open_blockers` | bool | |
| `is_archived` | bool | Defaults to `false`; archived projects are excluded unless asked for. |
| `q` | str | Case-insensitive substring over `code`, `name`, `client.alias`. |

Task-list filters: `assignee`, `priority` (`Priority.code`), `state`, `state_category`,
`is_overdue` (bool, derived from `due_date`, never from a stored flag), `q`.

Ordering is a single `order_by` param taking a signed field name from a per-endpoint allowlist.
`-` means descending. Anything outside the allowlist is `422`. Ordering is always stabilised by
appending `code` as the final key, so pagination cannot repeat or drop a row.

**Filter values are always taxonomy `code`s, never Spanish labels.** Labels are operator-editable
data; the API returns them for display and accepts only codes as input.

### 1.5 Error envelope

One shape for every non-2xx produced by the application, emitted by the single
`@api.exception_handler(DomainError)` registration in `config/api.py`:

```json
{
  "code": "transition_not_allowed",
  "message": "PRJ-01 cannot move from 'ejecucion' to 'hecho'.",
  "details": {
    "from_state": "ejecucion",
    "to_state": "hecho",
    "allowed": ["bloqueado", "pausado", "en_revision"]
  }
}
```

- `code` — stable machine string. **The frontend switches on `code` and never on `message`.**
- `message` — English, for logs and for a developer. It is not UI copy; the Spanish UI maps
  `code` to its own text.
- `details` — object, may be `{}`, never `null`. Shape is documented per error below.

| Domain error | HTTP | `code` | `details` | When |
|---|---|---|---|---|
| `NotFound` | 404 | `not_found` | `{entity, id}` | Unknown project/task/blocker identifier. |
| `TransitionNotAllowed` | 409 | `transition_not_allowed` | `{from_state, to_state, allowed[]}` | No active `WorkflowTransition` for that pair, or a guard rejected it. |
| `ConflictingState` | 409 | `conflicting_state` | `{entity, id, current}` | The operation is legal in the workflow but not against current facts: resolving an already-resolved blocker, archiving twice, overriding priority on an archived project. |
| `ValidationError` | 422 | `validation_error` | `{fields: {name: [msg]}}` | Missing `reason` when `requires_reason`, empty `requires_fields`, unparseable filter, unknown taxonomy code, dependency cycle. |
| any other `DomainError` | 400 | subclass-defined | `{}` | Fallback branch; adding an error means adding one row to `STATUS_BY_ERROR`. |

Ninja's own request-validation failures are re-shaped into the same envelope with
`code: "validation_error"` so the client has exactly one parser. `500` is not part of the
contract: it means a bug, and the body is not guaranteed.

Views never catch domain errors. A `try/except DomainError` inside a router is a bug.

### 1.6 Shared object shapes

Referenced by name throughout §2.

```
TaxonomyRef   { code: str, label: str, color: str | null }
CurrencyRef   { code: str, label: str, color: str | null, minor_units: int }
StateRef      { code: str, label: str, category: str, color: str | null }
ActorRef      { alias: str, label: str, role: str | null }
RiskFlag      { code: str, severity: "LOW"|"MEDIUM"|"HIGH"|"CRITICAL", reason: str }
HealthRef     { code: "HEALTHY"|"AT_RISK"|"BLOCKED", label: str }
ScoreSignal   { code: str, raw: float, weight: float, contribution: float, reason: str }
Score         { value: float, policy_version: int, computed_at: datetime,
                breakdown: ScoreSignal[], modifiers: {code: float}, flags: str[] }
Override      { position: int | null, boost: float | null, reason: str,
                actor: str, created_at: datetime, expires_at: datetime | null }
Transition    { to_state: StateRef, label: str, requires_reason: bool,
                requires_fields: str[] }
```

`RiskFlag` and `HealthRef` are **computed on read**, on every response that carries them
([ADR 0011](adr/0011-risk-flags-computed-on-read.md)). They are never stored, so they cannot be
stale: a project whose target date passed at midnight reads `OVERDUE` on the next request whether or
not any event was emitted, and there is no `project.risk.changed` frame on the stream because there
is no stored set for anything to change *from*. `health` is derived from the flags —
`BLOCKED` when any `CRITICAL` flag is raised, `AT_RISK` when any flag is raised, else `HEALTHY` — so
the two can never disagree.

`Score.value` is the computed 0–100 number and is **never** rewritten by an override. When
`override` is non-null the frontend labels the row as a manual override and still shows
`score.value` next to it (`ARCHITECTURE.md` §4.2).

Dates are ISO-8601. Datetimes are UTC with a `Z` suffix. Nulls are transmitted as `null` and mean
"absent", which is itself a signal — the client must not substitute a default.

## 2. Endpoints

### 2.1 `GET /api/v1/queue` — prioritized project queue

The command center's single read. Serves the `ProjectSnapshot` read model (`ARCHITECTURE.md` §8),
not the write aggregates.

Query: §1.3 pagination + §1.4 project filters. `order_by` allowlist:
`score` (default `-score`), `target_date`, `name`, `updated_at`.

Response `200: Paginated[QueueItemOut]`:

```
QueueItemOut {
  code, name: str
  client: TaxonomyRef
  owner: ActorRef
  engagement_type, project_type, stage: TaxonomyRef
  state: StateRef
  health: HealthRef              # computed per request, never stored
  target_date: date | null
  business_value: int, currency: str
  next_step: str | null
  open_tasks, overdue_tasks, open_blockers: int
  score: Score
  override: Override | null
  risk_flags: RiskFlag[]
  updated_at: datetime
}
```

```json
{
  "count": 22,
  "items": [
    {
      "code": "PRJ-01",
      "name": "Global Contract Management",
      "client": {"code": "atlas-foods", "label": "Atlas Foods", "color": null},
      "owner": {"alias": "daniel", "label": "Daniel Rojas", "role": "Commercial / Delivery"},
      "engagement_type": {"code": "proyecto", "label": "Proyecto", "color": "#3E63DD"},
      "project_type": {"code": "automatizacion", "label": "Automatizacion", "color": null},
      "stage": {"code": "ejecucion", "label": "Ejecucion", "color": null},
      "state": {"code": "ejecucion", "label": "En ejecucion", "category": "IN_PROGRESS", "color": "#3E63DD"},
      "health": {"code": "BLOCKED", "label": "Blocked"},
      "target_date": null,
      "business_value": 28000,
      "currency": "USD",
      "next_step": null,
      "open_tasks": 4,
      "overdue_tasks": 2,
      "open_blockers": 2,
      "score": {
        "value": 70.3,
        "policy_version": 1,
        "computed_at": "2026-07-28T06:00:00Z",
        "breakdown": [
          {"code": "blockage", "raw": 1.0, "weight": 0.15, "contribution": 15.0,
           "reason": "2 open blocker(s); the oldest has been open for 24 day(s) and needs intervention."},
          {"code": "business_value", "raw": 0.86, "weight": 0.15, "contribution": 12.9,
           "reason": "28000 USD, log-normalized against the portfolio range 4000-40000."},
          {"code": "deadline_pressure", "raw": 0.5, "weight": 0.25, "contribution": 12.5,
           "reason": "No target date recorded; neutral 0.5 applied and NO_TARGET_DATE raised."},
          {"code": "overdue_work", "raw": 0.5, "weight": 0.2, "contribution": 10.0,
           "reason": "2 of 4 open tasks are past due."},
          {"code": "criticality", "raw": 0.5, "weight": 0.15, "contribution": 7.5,
           "reason": "2 of 4 open tasks are Critica or Alta."},
          {"code": "staleness", "raw": 0.6, "weight": 0.1, "contribution": 6.0,
           "reason": "9 day(s) without recorded activity and no next step set."}
        ],
        "modifiers": {"engagement_type": 1.1},
        "flags": ["NO_TARGET_DATE"]
      },
      "override": null,
      "risk_flags": [
        {"code": "BLOCKED", "severity": "HIGH",
         "reason": "2 open blockers and 1 task in a BLOCKED state."},
        {"code": "OVERDUE", "severity": "HIGH",
         "reason": "2 tasks past due; the oldest by 18 day(s)."},
        {"code": "NO_TARGET_DATE", "severity": "MEDIUM",
         "reason": "Active project with no target date."},
        {"code": "NO_NEXT_STEP", "severity": "MEDIUM",
         "reason": "No next step recorded and no task in progress."}
      ],
      "updated_at": "2026-07-28T06:00:00Z"
    }
  ]
}
```

`breakdown` is returned sorted by `contribution` descending, which is the order the UI reads it
in. `sum(contribution) * product(modifiers)` equals `value` to one decimal.

### 2.2 `GET /api/v1/projects/{code}` — project detail

Query: none. `code` is the business identifier (`PRJ-01`), never a database primary key.

Response `200: ProjectDetailOut` = every field of `QueueItemOut` plus:

```
tasks: TaskOut[]                 # all non-archived tasks, ordered by priority weight then due_date
blockers: BlockerOut[]           # open first, then resolved, newest first
transitions: Transition[]        # the legal transitions from the current state, for this workflow
summary: str | null
start_date: date | null
is_archived: bool
```

```json
{
  "code": "PRJ-01",
  "name": "Global Contract Management",
  "summary": "Plataforma Integral de Gestion de Contratos para el Equipo Legal Global.",
  "state": {"code": "ejecucion", "label": "En ejecucion", "category": "IN_PROGRESS", "color": "#3E63DD"},
  "next_step": null,
  "start_date": null,
  "target_date": null,
  "is_archived": false,
  "tasks": [
    {
      "code": "PRJ-01-T02",
      "title": "Resolve priority issue in pilot or production - Global Contract Management",
      "assignee": {"alias": "daniel", "label": "Daniel Rojas", "role": "Commercial / Delivery"},
      "priority": {"code": "critica", "label": "Critica", "color": "#E5484D"},
      "state": {"code": "por_hacer", "label": "Por hacer", "category": "BACKLOG", "color": "#8B8D98"},
      "due_date": "2026-07-10",
      "is_overdue": true,
      "last_progress": "There is a user-visible issue affecting trust or adoption.",
      "dependencies": [{"task_code": null, "raw_label": "Functional validation and release checklist"}]
    },
    {
      "code": "PRJ-01-T03",
      "title": "Align external dependency with client or vendor - Global Contract Management",
      "assignee": {"alias": "daniel", "label": "Daniel Rojas", "role": "Commercial / Delivery"},
      "priority": {"code": "media", "label": "Media", "color": "#F5A524"},
      "state": {"code": "bloqueada", "label": "Bloqueada", "category": "BLOCKED", "color": "#E5484D"},
      "due_date": "2026-07-14",
      "is_overdue": false,
      "last_progress": "Waiting on client response, credentials, external API or business definition.",
      "dependencies": [{"task_code": "PRJ-01-T02", "raw_label": "Resolve priority issue in pilot or production"}]
    }
  ],
  "blockers": [
    {
      "id": 41,
      "kind": "EXTERNAL_DEPENDENCY",
      "description": "There are external dependencies or pending accesses.",
      "owner": {"alias": "daniel", "label": "Daniel Rojas", "role": "Commercial / Delivery"},
      "raised_at": "2026-07-04T09:12:00Z",
      "resolved_at": null,
      "age_days": 24,
      "task_code": null
    }
  ],
  "transitions": [
    {"to_state": {"code": "bloqueado", "label": "Bloqueado", "category": "BLOCKED", "color": "#E5484D"},
     "label": "Marcar como bloqueado", "requires_reason": true, "requires_fields": []},
    {"to_state": {"code": "en_revision", "label": "En revision", "category": "IN_PROGRESS", "color": "#3E63DD"},
     "label": "Pasar a revision", "requires_reason": false, "requires_fields": ["next_step"]},
    {"to_state": {"code": "pausado", "label": "Pausado", "category": "BACKLOG", "color": "#8B8D98"},
     "label": "Pausar", "requires_reason": true, "requires_fields": []}
  ]
}
```

`transitions` is the **only** source of transition buttons. The frontend never holds a list of
state codes and never guesses legality. A `requires_fields` entry that is currently empty on the
project means the button renders disabled with the field named.

Errors: `404 not_found`.

### 2.3 `POST /api/v1/projects` — create a project

Authentication: any member. Body `ProjectCreateIn`:

```
name: str                       # required, 1..200
client: str                     # required, Client.alias
engagement_type: str            # required, EngagementType.code
project_type: str               # required, ProjectType.code
stage: str                      # required, Stage.code
owner: str                      # required, accounts.User.code
start_date: date | null
target_date: date | null
business_value: int             # >= 0
currency: str                   # Currency.code (ISO-4217), default "USD"
summary: str | null
next_step: str | null
```

`code` is **not** accepted; the service allocates the next `PRJ-NN`. `workflow_state` is not
accepted either — the project starts in the `is_initial` state of the workflow that
`WorkflowBinding` resolves for its `engagement_type` (hard rule 2).

Response `201: ProjectDetailOut`. Emits `ActivityRecord(verb=CREATED)` and topic
`project.created`.

```json
{"name": "Contract Intake Automation", "client": "atlas-foods", "engagement_type": "proyecto",
 "project_type": "automatizacion", "stage": "descubrimiento", "owner": "laura",
 "start_date": "2026-08-03", "target_date": "2026-10-30", "business_value": 18000,
 "currency": "USD", "summary": "Automatizacion de la recepcion de contratos.",
 "next_step": "Agendar kickoff con Legal."}
```

`currency` is a taxonomy `code` like every other reference: it is resolved against
`catalog_currency` and an unknown one is a `422` on the `currency` field, never a silently stored
string. The list a client may send is `GET /api/v1/catalog` (§2.17).

Errors: `422 validation_error` (unknown taxonomy code, negative value, `target_date` before
`start_date`).

### 2.4 `PATCH /api/v1/projects/{code}` — update a project

Authentication: any member. Body `ProjectUpdateIn`: every field of `ProjectCreateIn` optional; absent
fields are untouched, explicit `null` clears a nullable field.

`workflow_state`, `health`, `score` and `code` are rejected with `422 validation_error`. State
moves through §2.5 only; health and score are derived, never written.

Response `200: ProjectDetailOut`. Emits one `ActivityRecord` per meaningful changed field
(`OWNER_CHANGED`, `NEXT_STEP_SET`, otherwise a generic update record) and topic
`project.updated`.

```json
{"next_step": "Confirmar accesos al repositorio de contratos con el cliente.", "owner": "camila"}
```

Errors: `404 not_found`, `422 validation_error`, `409 conflicting_state` (archived project).

### 2.5 `POST /api/v1/projects/{code}/transition` — execute a workflow transition

Authentication: any member. Body:

```
to_state: str          # required, WorkflowState.code, must be reachable from the current state
reason: str | null     # required and non-empty when the transition has requires_reason
fields: {str: any}     # optional; values for requires_fields set atomically with the move
```

Response `200: ProjectDetailOut` with the new state and the **new** legal `transitions`.

```json
{"to_state": "bloqueado", "reason": "Accesos al repositorio legal pendientes desde el 4 de julio."}
```

Emits `ActivityRecord(verb=STATE_CHANGED, from_value, to_value, reason)` and topic
`project.state_changed`, both inside the same transaction as the state change and the
`OutboxEvent`.

Errors:

- `409 transition_not_allowed` — no active `WorkflowTransition`, or a guard rejected it.
  `details.allowed` lists the legal `to_state` codes so the client can resync without a refetch.
- `422 validation_error` — missing `reason` (`details.fields.reason`), or a `requires_fields`
  entry still empty (`details.fields.next_step`).
- `404 not_found`.

### 2.6 `POST /api/v1/projects/{code}/priority-override` — manual override

Authentication: **ops lead** (§1.2.3). This is the one place a person overrules the ranking engine
for the whole board, and a queue anybody can reorder is not a prioritized queue. An authenticated
member who is not an ops lead gets `403 permission_denied` with
`details = {"required": "ops_lead", "action": "..."}`.

Body:

```
position: int | null   # 1-based forced position in the queue
boost: float | null    # additive points applied on top of the computed score
reason: str            # required, non-empty
expires_at: datetime | null
```

Exactly one of `position` / `boost` must be present. `reason` is a constraint, not a convention.

Response `200: { code, score: Score, override: Override }`. The computed `score.value` is
unchanged — the override is stored separately so the ranking stays auditable and reversible.
Emits `ActivityRecord(verb=PRIORITY_CHANGED, metadata.origin="MANUAL")`, correlated by
`correlation_id` with the record of any project displaced by the move, and topic
`project.priority.recalculated`.

```json
{"position": 1, "reason": "El cliente escalo a direccion; se atiende hoy.",
 "expires_at": "2026-08-04T00:00:00Z"}
```

```json
{
  "code": "PRJ-02",
  "score": {"value": 61.4, "policy_version": 1, "computed_at": "2026-07-28T06:00:00Z",
            "breakdown": [], "modifiers": {"engagement_type": 0.9}, "flags": ["NO_TARGET_DATE"]},
  "override": {"position": 1, "boost": null,
               "reason": "El cliente escalo a direccion; se atiende hoy.",
               "actor": "camila", "created_at": "2026-07-28T09:41:12Z",
               "expires_at": "2026-08-04T00:00:00Z"}
}
```

`DELETE /api/v1/projects/{code}/priority-override` removes it (`204`, **ops lead**, also recorded
as `PRIORITY_CHANGED`). Gated for symmetry: a rank one person may force and anybody may lift is a
suggestion, not a decision.

Errors: `422 validation_error` (empty reason, both or neither of `position`/`boost`),
`409 conflicting_state` (archived project), `404 not_found`.

### 2.7 `GET /api/v1/projects/{code}/tasks` — list tasks

Query: §1.3 pagination + task filters from §1.4. `order_by` allowlist: `due_date`, `priority`
(by `Priority.weight`), `state`, `code`. Default `-priority,due_date`.

Response `200: Paginated[TaskOut]`, `TaskOut` as shown in §2.2.

```json
{"count": 4, "items": [
  {"code": "PRJ-01-T01",
   "title": "Plan next delivery iteration - Global Contract Management",
   "assignee": {"alias": "daniel", "label": "Daniel Rojas", "role": "Commercial / Delivery"},
   "priority": {"code": "alta", "label": "Alta", "color": "#F76808"},
   "state": {"code": "en_progreso", "label": "En progreso", "category": "IN_PROGRESS", "color": "#3E63DD"},
   "due_date": "2026-07-12", "is_overdue": true,
   "last_progress": "Next iteration is being scoped with deliverables and owners.",
   "dependencies": []}
]}
```

`is_overdue` is derived from `due_date` against server time. The dataset's `Si`/`No` column is
not imported and is not part of this contract.

### 2.8 `POST /api/v1/projects/{code}/tasks` — create a task

Authentication: any member. Body:

```
title: str                 # required
detail: str | null
assignee: str | null       # accounts.User.code
priority: str              # required, Priority.code
due_date: date | null
depends_on: str[]          # task codes within the same project; unresolved text goes to raw_label
```

`workflow_state` is not accepted: the task starts in the `is_initial` state of the task workflow.
`code` is allocated as `{project_code}-T{NN}`.

Response `201: TaskOut`. Emits `ActivityRecord(verb=TASK_ADDED)` on the project and topic
`task.created`.

```json
{"title": "Validar checklist de release con Legal", "priority": "alta",
 "assignee": "daniel", "due_date": "2026-08-07", "depends_on": ["PRJ-01-T02"]}
```

Errors: `422 validation_error` — unknown `priority`, `depends_on` referencing a task in another
project, or a dependency cycle (`details.fields.depends_on`).

### 2.9 `POST /api/v1/tasks/{code}/transition` — transition a task

Authentication: any member. Body and error contract identical to §2.5, against the task workflow.

Response `200: TaskOut`. Emits `ActivityRecord(verb=STATE_CHANGED)` on the task and topic
`task.state_changed`. A task entering or leaving a `BLOCKED`-category state changes the project's
derived health, and nothing has to be written for that to be true — health is computed whenever the
project is read. What the event does cause is a rescore and a read-model rebuild; the browser
re-reads the project from `task.state_changed` itself.

```json
{"to_state": "bloqueada", "reason": "Sin credenciales del entorno del cliente."}
```

### 2.10 `POST /api/v1/projects/{code}/blockers` — raise a blocker

Authentication: any member. Body:

```
kind: str              # EXTERNAL_DEPENDENCY | ACCESS | DECISION | TECHNICAL
description: str       # required
owner: str | null      # accounts.User.code responsible for clearing it
task_code: str | null  # attach to a task instead of the project
```

Response `201: BlockerOut` (shape in §2.2). Emits `ActivityRecord(verb=BLOCKER_RAISED)` and topic
`blocker.raised`.

```json
{"kind": "ACCESS", "description": "Pendiente el acceso al repositorio de contratos del cliente.",
 "owner": "daniel"}
```

```json
{"id": 57, "kind": "ACCESS",
 "description": "Pendiente el acceso al repositorio de contratos del cliente.",
 "owner": {"alias": "daniel", "label": "Daniel Rojas", "role": "Commercial / Delivery"},
 "raised_at": "2026-07-28T10:02:41Z", "resolved_at": null, "age_days": 0, "task_code": null}
```

### 2.11 `POST /api/v1/blockers/{id}/resolve` — resolve a blocker

Authentication: any member. Body: `{ "resolution": str }` — required, non-empty; it is stored as the
`ActivityRecord.reason`.

Response `200: BlockerOut` with `resolved_at` set. Emits
`ActivityRecord(verb=BLOCKER_RESOLVED)` and topic `blocker.resolved`. A project whose last open
blocker is resolved loses the `BLOCKED` risk flag **immediately** — the flag *is* "an open blocker
exists", so the next read of the project no longer raises it. The API does not change the workflow
state as a side effect.

Errors: `409 conflicting_state` when already resolved (`details.current = "resolved"`),
`404 not_found`, `422 validation_error`.

### 2.12 `POST /api/v1/projects/{code}/notes` — add a note

Authentication: any member. Body: `{ "body": str, "task_code": str | null }`.

Response `201: { id, body, author: ActorRef, created_at, task_code }`. Emits
`ActivityRecord(verb=NOTE_ADDED)` and topic `note.added`. Notes are chronological comments; they
are not a substitute for a `Blocker`, and nothing parses their text.

### 2.13 `GET /api/v1/projects/{code}/activity` — timeline

Query: §1.3 pagination, plus `verb` (repeatable, from the `ActivityRecord.verb` set),
`since` / `until` (datetime), `correlation_id` (uuid). Ordered `-occurred_at` only.

Response `200: Paginated[ActivityOut]`:

```
ActivityOut {
  id: int
  entity: { type: "project"|"task"|"blocker", id: str }
  verb: str
  actor: str
  from_value: str | null
  to_value: str | null
  reason: str | null
  metadata: object
  occurred_at: datetime
  correlation_id: str
}
```

```json
{"count": 3, "items": [
  {"id": 812, "entity": {"type": "project", "id": "PRJ-01"}, "verb": "STATE_CHANGED",
   "actor": "camila", "from_value": "ejecucion", "to_value": "bloqueado",
   "reason": "Accesos al repositorio legal pendientes desde el 4 de julio.",
   "metadata": {"workflow": "project-default"},
   "occurred_at": "2026-07-28T09:48:03Z",
   "correlation_id": "8b0f6c1e-0f8b-4a5e-9a63-2f7c9b1de111"},
  {"id": 811, "entity": {"type": "project", "id": "PRJ-01"}, "verb": "PRIORITY_CHANGED",
   "actor": "system", "from_value": "63.9", "to_value": "70.3",
   "reason": "blockage rose from 0.62 to 1.00: blocker #41 open for 24 days.",
   "metadata": {"origin": "POLICY", "policy_version": 1, "signal": "blockage"},
   "occurred_at": "2026-07-28T06:00:02Z",
   "correlation_id": "1c4d2a90-77f1-42b6-8b0e-6b5b3a2f0004"},
  {"id": 640, "entity": {"type": "project", "id": "PRJ-01"}, "verb": "SEEDED",
   "actor": "system", "from_value": null, "to_value": "ejecucion", "reason": null,
   "metadata": {"fixture": "portfolio"}, "occurred_at": "2026-07-01T00:00:00Z",
   "correlation_id": "0d0a5c8a-3b6f-4a19-9a8c-b0b6bb6f0640"}
]}
```

`metadata.origin` distinguishes `MANUAL` from `POLICY` on every `PRIORITY_CHANGED` row. Records
sharing a `correlation_id` are one decision ("deprioritize A to prioritize B") and the UI groups
them. The timeline is append-only: there is no `PATCH` or `DELETE` on it.

### 2.14 `GET /api/v1/team/load` — load per person

Computed from `Task` rows at read time. The `Team` sheet counters in the source dataset are a
stale projection and are not imported.

Query: `owner` (repeatable), `include_inactive` (bool, default `false`). Not paginated: five rows.

Response `200: { items: TeamLoadOut[] }`:

```
TeamLoadOut {
  alias, label: str
  role: str | null
  weekly_capacity_points: int
  load_points: int              # sum of Priority.weight over open assigned tasks
  utilization: float            # load_points / weekly_capacity_points
  is_overloaded: bool           # utilization > 1.0 -> OWNER_OVERLOADED on their projects
  open_tasks, blocked_tasks, high_or_critical_open, overdue_tasks: int
  projects_owned: int
}
```

```json
{"items": [
  {"alias": "camila", "label": "Camila Torres", "role": "Delivery",
   "weekly_capacity_points": 40, "load_points": 62, "utilization": 1.55, "is_overloaded": true,
   "open_tasks": 28, "blocked_tasks": 7, "high_or_critical_open": 20, "overdue_tasks": 12,
   "projects_owned": 7},
  {"alias": "daniel", "label": "Daniel Rojas", "role": "Commercial / Delivery",
   "weekly_capacity_points": 40, "load_points": 24, "utilization": 0.6, "is_overloaded": false,
   "open_tasks": 11, "blocked_tasks": 2, "high_or_critical_open": 6, "overdue_tasks": 5,
   "projects_owned": 3}
]}
```

Overload never lowers a project's score. It raises `OWNER_OVERLOADED` on that owner's projects,
which is a staffing decision, not a ranking one.

### 2.15 `POST /api/v1/projects/{code}/recompute` and `POST /api/v1/recompute`

The manual override of an automatic ranking, and the replacement for `manage.py recompute`. A
command run from a laptop against the production database has no audit trail, no permission
boundary and no record that it happened; the same use case over HTTP runs inside the deployment.

Authentication: **ops lead** for `POST /recompute`; any member for `POST /projects/{code}/recompute`. Body: none.

Two routes rather than one route with an optional project, because "rescore this" and "rescore
everything" have different blast radii and the client should have to say which it meant. The
portfolio route skips archived projects.

Response `200: RecomputeOut`:

```
RecomputeOut {
  ran_at: datetime               # the single instant every item was scored for
  changed: int                   # how many projects actually moved
  items: [{ project_code: str, value: float, health: str,
            flags: str[], changed: bool }]
}
```

```json
{"ran_at": "2026-07-28T09:41:12Z", "changed": 1, "items": [
  {"project_code": "PRJ-01", "value": 70.3, "health": "BLOCKED",
   "flags": ["BLOCKED", "OVERDUE", "NO_TARGET_DATE"], "changed": true}
]}
```

**Emits no event and writes no `ActivityRecord`.** A rebuild is derivation, not a decision, and
broadcasting `project.priority.recalculated` for twenty-two projects that did not move would tell
every open dashboard that something happened when nothing did. `changed` is how the caller finds
out either way — `22 recomputed, 0 changed` is a healthy system confirming itself.

Errors: `401 authentication_required`, `403 permission_denied` (portfolio route, non-ops-lead),
`404 not_found` (unknown project), `422 validation_error` (no `PriorityPolicy` is active).

### 2.16 Health — `GET /api/v1/health/{live,ready,pipeline}`

Three routes because an orchestrator asks three different questions, and answering them with one
endpoint is how a dependency outage becomes an application outage. **These three are the only reads
on the API that are unauthenticated** (§1.2): a probe runs before anything can present a token, and
none of the three reveals business data.

| Route | Checks | Status | Who reads it |
|---|---|---|---|
| `/health/live` | **Nothing.** | always `200` | A kubelet liveness probe. Compose defines no healthcheck. |
| `/health/ready` | PostgreSQL, Redis | `200` / `503` | The load balancer, to drain traffic. |
| `/health/pipeline` | Outbox, dead letters, Beat | **always `200`** | A human, or a dashboard scraper. |

`/health/live` checks no dependency, ever. If liveness touched Redis, a Redis outage would fail the
probe, Docker would restart the API, the restart would not fix Redis, and the one process still
able to serve cached reads would be in a crash loop for the duration of the incident. Liveness is
about the process; readiness is about the request.

```json
{"status": "alive"}
```

`/health/ready` returns the same body shape on both statuses, so one parser covers both, and every
check runs even after one has failed — "the database is down" and "both are down" call for
different responses.

```json
{"ready": false, "checks": [
  {"name": "database", "ok": true, "detail": "ok"},
  {"name": "redis", "ok": false, "detail": "Error 111 connecting to redis:6379. Connection refused."}
]}
```

`/health/pipeline` is informational and **always 200, including when every number is alarming**. It
must never gate a container healthcheck: a backlog or a poisoned event is a fact about the workers,
and restarting the API because a handler is stuck is the exact inversion of what an operator wants
during an incident. It is also what replaced `XPENDING` when Celery became the transport — the
outbox is a PostgreSQL table, so this is a query rather than a redis-cli session.

```
PipelineOut {
  unpublished: int                            # outbox rows the drain has not dispatched
  oldest_unpublished_age_seconds: float|null  # null = empty backlog, never 0
  dead_lettered: int                          # rows past the attempt budget; re-queued from /admin/
  last_tick_at: datetime | null               # newest clock.ticked row = proof Beat is alive
  last_tick_age_seconds: float | null
}
```

```json
{"unpublished": 0, "oldest_unpublished_age_seconds": null, "dead_lettered": 0,
 "last_tick_at": "2026-07-28T09:40:00Z", "last_tick_age_seconds": 72.4}
```

Read the numbers together: `unpublished` rising with its age means the drain is not running, while
rising with a low age is a burst being worked through; `last_tick_at` older than
`TICKER_INTERVAL_SECONDS` means Beat is down, which is otherwise silent — nothing fails, scores
simply stop ageing. A `null` age means "nothing to measure" and is deliberately not `0`, which
would read as "perfectly fresh" for exactly the state that is most suspicious.

### 2.17 `GET /api/v1/catalog` — the taxonomies the client renders pickers from

Authenticated like every other read — the vocabulary is not secret, but it is not public either,
and the unauthenticated list is five routes long and closed (§1.2). One document rather than six
endpoints, because a form needs every list before it can draw itself.

Response `200: CatalogOut`:

```
CatalogOut {
  engagement_types, project_types, stages, priorities, roles: TaxonomyRef[]
  currencies: CurrencyRef[]
}
CurrencyRef {                    # TaxonomyRef plus one field
  code: str                      # ISO-4217 alphabetic, upper case
  label: str
  color: str | null
  minor_units: int               # 0..4 — decimal places the amount is written with
}
```

```json
{"priorities": [{"code": "critica", "label": "Critica", "color": "#dc2626"}],
 "currencies": [{"code": "USD", "label": "Dolar estadounidense", "color": "#16a34a",
                 "minor_units": 2},
                {"code": "CLP", "label": "Peso chileno", "color": "#b91c1c",
                 "minor_units": 0}]}
```

Only **active** rows are served, in the operator's own `order`. A retired value keeps resolving on
the projects that already point at it (that is what `is_active` is for) but must not reappear in a
picker where somebody could choose it again.

`minor_units` is the reason currencies are a taxonomy at all. It is the one field a client cannot
derive — `28000` is `$280.00` in USD and `$28.000` in CLP — so a frontend formatting with a
hardcoded 2 is wrong by two orders of magnitude for every zero-decimal currency.

**Workflow states are deliberately absent.** A state code is unique only inside its workflow, and
the legal moves out of the state a project is actually in are `transitions` on the project detail
(§2.2). A global list of states would invite the client to guess legality, which is exactly what
that field exists to prevent.

## 3. `GET /api/stream` — server-sent events

Unversioned, always-on, served by the ASGI app. One connection per browser tab, owned by
`frontend/src/lib/stream/store.ts`; islands subscribe to the store and never construct an
`EventSource`.

### 3.1 Request

```
GET /api/stream?topics=project.state_changed,blocker.raised&project=PRJ-01&last_event_id=018f...
Accept: text/event-stream
Last-Event-ID: 018f2c7a-5f6b-7c31-9a1e-0f6d2b4c8a11
```

Query params, all optional and typed:

| Param | Type | Meaning |
|---|---|---|
| `topics` | comma-separated | Restrict to these topics. Default: the whole fan-out allowlist. |
| `project` | str | Only events whose `entity.id` is this project code, plus its tasks and blockers. |
| `last_event_id` | uuid | Same effect as the `Last-Event-ID` header; used by a manual reconnect, which builds a new `EventSource` that does not carry the header. |

**Authenticated, and this endpoint is the reason the access token is also a cookie.** `EventSource`
cannot set request headers, so a browser opens it with `withCredentials: true` and the browser
attaches `aztec_access` by itself (§1.2.2); a non-browser client sends `Authorization: Bearer`. A
token in the query string is refused — `?token=` lands in every proxy access log on the path.

The stream is not actor-scoped: every subscriber sees the same board, because the board is shared.
It is not public either — it broadcasts project names, blockers and client identities — so a
missing or refused credential is answered with the §1.5 envelope and a `401`, **before** the
`text/event-stream` response is opened. Returning `200` and then closing would make a rejected
credential look like a flaky connection, and the browser would retry it on a timer forever.

### 3.2 Response headers

```
200 OK
Content-Type: text/event-stream; charset=utf-8
Cache-Control: no-cache, no-transform
Connection: keep-alive
X-Accel-Buffering: no
```

No `Content-Length`, no compression. `X-Accel-Buffering: no` is what stops a proxy from holding
frames until a buffer fills.

### 3.3 Frame format

Every frame is:

- `id:` — the envelope `id` (a uuid). The browser stores it and replays it as `Last-Event-ID`.
- `event:` — the topic, so `es.addEventListener(topic, ...)` works per topic.
- `data:` — the envelope from `ARCHITECTURE.md` §6, on a single line, terminated by a blank line.

The envelope is fixed. New information goes inside `payload`; changing the top level means
bumping `version` and handling both versions in every consumer and in the store.

### 3.4 Heartbeat

A comment frame every **15 seconds** while idle:

```
: ping
```

Comments carry no `id`, so they never disturb `Last-Event-ID`. A client that has seen neither an
event nor a heartbeat for 45 seconds treats the connection as dead and reconnects. The server
also sends `retry: 3000` once, immediately after the connection opens, as the browser's own
backoff floor.

### 3.5 Reconnection

- On reconnect the browser sends `Last-Event-ID` automatically, and a manual reconnect (the retry
  control in the disconnected state) builds a new `EventSource`, which does not carry the header —
  so it must pass `?last_event_id=` from the id the store kept.
- **There is no replay.** The fan-out is Redis pub/sub, which has no history, so an id the server
  cannot resume from is answered with a single `event: stream.reset` frame carrying
  `{"reason": "last_event_id_expired"}`. On that frame the client refetches the affected resources
  instead of trusting its local state. Real replay would mean a durable fan-out channel — a change
  to the transport contract, not to this endpoint. The escape hatch is deliberate: the outbox is
  the durable log, and `GET /api/v1/projects` is how a client catches up.
- Delivery is at-least-once. The same `event.id` will arrive twice; the store deduplicates on it,
  and handlers must be safe to run twice.

### 3.6 Forwarded topics

The `sse-fanout` handler publishes only what the UI reacts to:

`project.created`, `project.updated`, `project.state_changed`, `project.priority.recalculated`,
`task.created`, `task.updated`, `task.state_changed`, `blocker.raised`, `blocker.resolved`,
`note.added`.

There is deliberately no risk topic. Flags are computed on read, so a client re-reads the project
named by any of the above and gets its current flags with it; the only change with no frame behind
it is the calendar ([ADR 0011](adr/0011-risk-flags-computed-on-read.md)).

Anything not on this list is still dispatched to its handlers and still recorded in the outbox; it
simply never reaches the browser. `clock.ticked` is the standing example — the tick renders nothing,
and what it *causes* arrives as its own event. Forwarding a topic means adding it to
`EVENTS.md` §4/§5, to `SSE_ALLOWLIST_TOPICS`, and to `frontend/src/lib/stream/topics.ts`, in the
same change.

### 3.7 Worked example — raw wire bytes

The transition from §2.5, as it leaves `GET /api/stream`. `\n` shown explicitly; the frame ends
with a blank line, i.e. two consecutive newlines.

```
retry: 3000\n
\n
: ping\n
\n
id: 018f2c7a-5f6b-7c31-9a1e-0f6d2b4c8a11\n
event: project.state_changed\n
data: {"id":"018f2c7a-5f6b-7c31-9a1e-0f6d2b4c8a11","topic":"project.state_changed","occurred_at":"2026-07-28T09:48:03Z","actor":"camila","correlation_id":"8b0f6c1e-0f8b-4a5e-9a63-2f7c9b1de111","entity":{"type":"project","id":"PRJ-01"},"payload":{"from":"ejecucion","to":"bloqueado","reason":"Accesos al repositorio legal pendientes desde el 4 de julio."},"version":1}\n
\n
```

The follow-up frame the recalculator produces moments later, same `correlation_id`:

```
id: 018f2c7a-6a02-7d10-b3c7-11e9d0c4f221\n
event: project.priority.recalculated\n
data: {"id":"018f2c7a-6a02-7d10-b3c7-11e9d0c4f221","topic":"project.priority.recalculated","occurred_at":"2026-07-28T09:48:04Z","actor":"system","correlation_id":"8b0f6c1e-0f8b-4a5e-9a63-2f7c9b1de111","entity":{"type":"project","id":"PRJ-01"},"payload":{"from":70.3,"to":74.8,"policy_version":1,"origin":"POLICY","signal":"blockage"},"version":1}\n
\n
```

`data:` must never contain a raw newline. The envelope is serialised with no indentation; a
multi-line body would need one `data:` line per fragment and the client would have to rejoin them.

## 4. Contract versus internal

### 4.1 The frontend may rely on these

- Path and shape of every route in §2, the `/api/v1` prefix, and `/api/stream`.
- The pagination envelope `{items, count}` and the `page` / `page_size` params.
- The error envelope `{code, message, details}` and the `code` values in the §1.5 table.
  Branching happens on `code`.
- The event envelope's top level (`id`, `topic`, `occurred_at`, `actor`, `correlation_id`,
  `entity`, `payload`, `version`), the SSE frame format, `Last-Event-ID` semantics and the topic
  list in §3.6.
- `entity.id` is always the business code (`PRJ-01`, `PRJ-01-T02`), never a database primary key.
- Every taxonomy and state reference arrives as `{code, label, color}` / `{code, label, category,
  color}`. The client renders `label` and `color`, and compares only `code` and `category`.
- `transitions` on the project detail is the complete and only set of legal moves.
- Every score is delivered with its `breakdown`; an override never overwrites `score.value`.
- Absent values are `null` and mean absent.

### 4.2 Internal — do not depend on these

- Numeric primary keys (`Blocker.id`, `ActivityRecord.id`) beyond passing them straight back to
  the route that issued them. They are not stable across a reseed.
- The signal codes and weights inside `breakdown`, and `policy_version`. They change when a new
  `PriorityPolicy` version is activated; the UI renders whatever entries arrive and must not
  hardcode the six current ones.
- `RiskFlag.code` values as a closed set. New specifications add codes (`CLAUDE.md` rule 8); the
  UI renders unknown codes with their `reason` rather than dropping them.
- The wording of `reason` strings and of `message`. They are generated text, not identifiers.
- `metadata` on `ActivityRecord` — free-form JSONB, per-verb, and it evolves.
- `ProjectSnapshot`, the outbox table, the Celery queues and the handler registry. None of them are
  addressable over HTTP; the SSE endpoint is the only live window onto the bus, and
  `GET /api/v1/health/pipeline` is the only aggregate one. The dead-letter re-queue lives in the
  admin, deliberately: replaying an event is an operator action with a person behind it.
- Ordering of any list beyond the documented default and the `order_by` allowlist.
- The Django admin at `/admin/`. It is an operator tool, not an API.
