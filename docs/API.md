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

**Level 2 — ops lead.** Three capabilities, and each one is upstream of the work rather than part
of it: the first overrules the engine rather than feeding it, the second decides who is in the
operation at all, and the third shapes the lifecycles every record then has to obey.

| Route | Why it is gated |
|---|---|
| `POST /projects/{code}/priority-override` | Forces the ranking against the computed score. |
| `DELETE /projects/{code}/priority-override` | The other half of the same capability: a rank one person may force and anybody may lift is a suggestion, not a decision. |
| `POST /recompute` | Portfolio-wide and expensive. `POST /projects/{code}/recompute` is *not* gated — its blast radius is one project. |
| `POST` / `PATCH` / `DELETE /team/members{,/…}` | Who is on the roster, and what capacity they are judged against — the divisor of every `OWNER_OVERLOADED` flag. |
| `POST /team/members/{code}/password` | Replacing somebody else's credential. |
| `POST /catalog/roles`, `PATCH /catalog/roles/{code}` | The one taxonomy writable outside `/admin/` (§2.16). |
| `POST` / `PATCH` / `DELETE /workflows/…` | Authoring a lifecycle changes what everybody else may do with every record on it (§2.19). |
| `PUT` / `DELETE /tasks/{code}/workflow` | Which lifecycle one task follows (§2.20). |

Deliberately **not** gated: `PUT`/`DELETE /projects/{code}/workflow`. Putting one project on an
existing lifecycle is reachable anyway by editing its `engagement_type` through §2.4, which is any
member's — so gating one of the two doors protected nothing. §2.20 argues it in full.

The 403 names which of the three was refused in `details.action`, so an operator who pressed
"Añadir persona" is not told they lack permission to override a ranking.

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
`is_overdue` (bool, derived from `due_date`, never from a stored flag), `is_archived`, `q`.

`is_archived` on a task list is the **scope**, not a facet, and it works exactly as it does on the
project surfaces: it defaults to `false`, and `true` returns the removed tasks *instead of* the
live ones rather than in addition to them ([ADR 0012](adr/0012-soft-delete-for-tasks.md)). It is
the only way back to a removed task, and therefore the read the restore control is built on.

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
RiskFlag      { code: str, severity: "LOW"|"MEDIUM"|"HIGH"|"CRITICAL", label: str, reason: str }
HealthRef     { code: "HEALTHY"|"AT_RISK"|"BLOCKED", label: str }
ScoreSignal   { code: str, label: str, raw: float, weight: float, contribution: float, reason: str }
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

`RiskFlag.label` and `RiskFlag.reason` are the Spanish the client renders (`PRODUCT.md`); `code` and
`severity` are the identifiers it branches on. The words travel on the wire because the frontend may
not hold an object literal keyed by a flag code (`docs/standards/FRONTEND.md` §7) — a seventh
specification would ship a code that map does not have, and the flag would render as `NO_TARGET_DATE`
or vanish. The same holds for `HealthRef.label`.

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
      "health": {"code": "BLOCKED", "label": "Bloqueado"},
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
          {"code": "blockage", "label": "Bloqueo", "raw": 1.0, "weight": 0.15, "contribution": 15.0,
           "reason": "2 bloqueo(s) abierto(s); el más antiguo lleva 24 día(s) sin resolverse y necesita intervención."},
          {"code": "business_value", "label": "Valor de negocio", "raw": 0.86, "weight": 0.15, "contribution": 12.9,
           "reason": "Valor de contrato 28.000,00 USD, normalizado logarítmicamente contra el máximo del portafolio (40.000,00)."},
          {"code": "deadline_pressure", "label": "Presión de fecha", "raw": 0.5, "weight": 0.25, "contribution": 12.5,
           "reason": "Sin fecha comprometida, la presión de fecha no se puede evaluar."},
          {"code": "overdue_work", "label": "Trabajo vencido", "raw": 0.5, "weight": 0.2, "contribution": 10.0,
           "reason": "2 de 4 tareas abiertas pasaron su fecha."},
          {"code": "criticality", "label": "Criticidad", "raw": 0.5, "weight": 0.15, "contribution": 7.5,
           "reason": "2 tarea(s) abierta(s) de prioridad urgente."},
          {"code": "staleness", "label": "Inactividad", "raw": 0.6, "weight": 0.1, "contribution": 6.0,
           "reason": "Sin actividad registrada durante 9 día(s) frente a un umbral de 14 día(s); no hay próximo paso registrado."}
        ],
        "modifiers": {"engagement_type": 1.1},
        "flags": ["NO_TARGET_DATE"]
      },
      "override": null,
      "risk_flags": [
        {"code": "BLOCKED", "severity": "CRITICAL", "label": "Bloqueado",
         "reason": "2 bloqueos abiertos; el más antiguo lleva 24 días."},
        {"code": "OVERDUE", "severity": "HIGH", "label": "Vencido",
         "reason": "2 tareas pasaron su fecha de vencimiento."},
        {"code": "NO_TARGET_DATE", "severity": "MEDIUM", "label": "Sin fecha objetivo",
         "reason": "No hay fecha objetivo comprometida."},
        {"code": "NO_NEXT_STEP", "severity": "MEDIUM", "label": "Sin próximo paso",
         "reason": "No hay próximo paso registrado ni ninguna tarea en curso."}
      ],
      "updated_at": "2026-07-28T06:00:00Z"
    }
  ]
}
```

`breakdown` is returned sorted by `contribution` descending, which is the order the UI reads it
in. `sum(contribution) * product(modifiers)` equals `value` to one decimal. Each line names itself:
`label` is what the signal measures and `reason` the sentence that defends the number, both already
in the interface's language, so the client never maps a signal `code` to text of its own.

### 2.2 `GET /api/v1/projects/{code}` — project detail

Query: none. `code` is the business identifier (`PRJ-01`), never a database primary key.

Response `200: ProjectDetailOut` = every field of `QueueItemOut` plus:

```
tasks: TaskOut[]                 # all non-archived tasks, ordered by priority weight then due_date
blockers: BlockerOut[]           # open first, then resolved, newest first
transitions: Transition[]        # the legal transitions from the current state, for this workflow
workflow: WorkflowRef            # {code, name, source: "DIRECT"|"INHERITED"} — §2.20
notes: NoteOut[]                 # the project's comments, newest first, up to 100
summary: str | null
start_date: date | null
is_archived: bool
```

```
NoteOut { id: int, code: str, body: str, author: str, created_at: datetime, task_code: str | null }
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
  ],
  "notes": [
    {"id": 391, "code": "NOTE-0391", "body": "Accesos solicitados al equipo legal; respuesta esperada el lunes.",
     "author": "camila", "created_at": "2026-07-28T10:04:00Z", "task_code": null},
    {"id": 388, "code": "NOTE-0388", "body": "El cliente confirmo el alcance del piloto.",
     "author": "daniel", "created_at": "2026-07-27T16:20:00Z", "task_code": "PRJ-01-T02"}
  ]
}
```

`transitions` is the **only** source of transition buttons. The frontend never holds a list of
state codes and never guesses legality. A `requires_fields` entry that is currently empty on the
project means the button renders disabled with the field named.

`notes` carries the project's comments newest first, capped at 100 — a ceiling, not a page, so the
panel never needs a second request to learn there is no second page. It **includes the notes
written against this project's tasks** (`task_code` names the task; `null` means the note was
written against the project itself), because `project` is copied onto a task note when it is
written and the `note.added` envelope names the project either way — a project-only read would make
a note that had appeared live disappear on reload. `author` is a bare string, not an `ActorRef`: it
is a denormalized code that outlives its author's row and can be `system`. An empty array means the
project has no commentary. Notes travel inside the detail rather than behind their own read for the
same reason `tasks` and `blockers` do: two reads can straddle a write, and the second request is
the one that fails after the first has already painted.

Errors: `404 not_found`.

### 2.2.1 `GET /api/v1/clients` — the counterparties a project can be registered against

Authentication: any member. Query: none.

```json
{"items": [{"code": "atlas-foods", "label": "Atlas Foods", "color": null}]}
```

Each entry is the same `{code, label, color}` reference shape as a taxonomy value (§1.6). A client
is not a taxonomy row, but on the wire it answers the same question — which of these do I choose —
and a fourth reference shape would be one more thing the frontend has to learn for no gain.

**Why here and not on `GET /api/v1/catalog`.** `Client` is a portfolio aggregate. The catalog is
the operator-editable *vocabulary*; a counterparty is a party. Publishing it from §2.17 would make
the catalog context read a model another context owns (ARCHITECTURE §3.3).

Retired counterparties (`is_active = false`) are absent, for the reason §2.17 gives: retirement
exists to take a value out of the pickers, so serving one would let an operator choose it again.
The projects already pointing at a retired client keep resolving through their foreign key.

An `{items: [...]}` object rather than a bare array, so the endpoint can grow a sibling key without
becoming a breaking change for a client that parsed the top level as a list.

### 2.3 `POST /api/v1/projects` — create a project

Authentication: any member. Body `ProjectCreateIn`:

```
name: str                       # required, 1..200
client: str                     # required, Client.code — see GET /api/v1/clients (§2.2.1)
engagement_type: str            # required, EngagementType.code
project_type: str | null        # ProjectType.code
stage: str | null               # Stage.code
owner: str | null               # accounts.User.code
start_date: date | null
target_date: date | null
business_value: int             # >= 0
currency: str                   # Currency.code (ISO-4217), default "USD"
summary: str                    # one line, default ""
description: str                # long-form Markdown, default ""
next_step: str                  # default ""
```

`description` is Markdown and is stored as Markdown, never as HTML — the editor that writes it
disables HTML pass-through, which is the sanitization boundary. It is the empty string when absent,
not null: a project without a description has an empty document, not a missing one.

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

`description` is updatable here and is **not** one of the nullable fields: the column is NOT NULL
with an empty default, so clearing a description is sending `""`. An explicit `null` is ignored
rather than turned into an empty string that would look like a deliberate blanking.

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

Removed tasks are absent from both `items` and `count`. `?is_archived=true` returns them instead
(§1.4), each carrying `is_archived: true`, which is the screen a task is restored from.

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
project, or a dependency cycle (`details.fields.depends_on`). `404 not_found` — a `depends_on`
naming a task that was removed: a new task may not be made to wait on work nothing will ever move.

### 2.8.1 `GET /api/v1/tasks/{code}` — one task, whole

Authentication: any member. No query parameters.

Response `200: TaskDetailOut` — the task's own fields plus `project` (`{code, name}`),
`dependencies`, **`transitions`**, `workflow` and `notes` (its own comments, newest first, up to
100). `transitions` is the only source of the state buttons on the task screen and is read from
`WorkflowTransition`, the same table §2.8.3's transition route validates against, so the two can
never disagree. An empty `transitions` means the task sits in a terminal state.

`workflow` is `{code, name, source: "DIRECT"|"INHERITED"}` — the lifecycle this task follows and
whether an ops lead chose it (§2.20). Two tasks of one project may legitimately follow different
graphs, so the screen has to be able to say why the buttons differ from the task beside it.

`notes` travels inside rather than behind a second route: a screen that needs two requests to
render is two chances to render half a page, and two reads can straddle a write.

Errors: `404 not_found` — the code names no task, **or names one that was removed**. A removed
task has no screen and no legal moves ([ADR 0012](adr/0012-soft-delete-for-tasks.md)); it is
reached through `GET /projects/{code}/tasks?is_archived=true`.

### 2.8.2 `PATCH /api/v1/tasks/{code}` — edit a task

Authentication: any member. Body — every field optional, absent means untouched, explicit `null`
clears:

```
title: str
detail: str
description: str
last_progress: str
priority: str            # Priority.code
assignee: str | null     # accounts.User.code; null unassigns
due_date: date | null
is_archived: bool        # false restores a removed task — see §2.8.3
depends_on: [str] | null # the WHOLE prerequisite set; the list replaces it, [] clears it
```

`workflow_state` is not a field of this payload: a state moves through §2.9 and nowhere else.
Neither is `is_overdue`, which is derived. `null` is accepted only where the column is nullable —
sending `null` for `title`, `detail`, `last_progress` or `priority` is a `422`.

`depends_on` is the one field that is **not** a patch of a scalar: it is a set, sent whole, and the
list it carries becomes the task's entire prerequisite set — entries not re-sent are removed, and
`[]` leaves the task waiting on nothing. Patching one entry of a list has no unambiguous spelling,
which is the same reasoning §2.19 gives for `requires_fields`, and here it is stronger: an edge that
is still only prose has no identifier a delta could name it by. Absent leaves the edges alone, and
`null` means the same as absent since `[]` already says "clear". Each entry is read exactly as in
§2.8 — a task code of the same project resolves to a real edge, anything else is kept verbatim as
the operation's own words. Re-sending an edge the task already has keeps its row, so an edit that
only adds a prerequisite does not restart the clock on the others.

Response `200: TaskDetailOut`, the whole detail rather than the changed fields, because the screen
that issued the edit is the screen that has to redraw. It is read back **unscoped**: a `PATCH`
that just moved `is_archived` in either direction returns the task as it now stands, never a 404.

Moving `priority` or `assignee` emits `ActivityRecord(verb=PRIORITY_CHANGED | OWNER_CHANGED)` on
the task and redrawing `depends_on` emits `DEPENDENCIES_CHANGED`, whose `metadata.before` /
`metadata.after` carry the full lists; a text or date edit emits none, on purpose. Any real change
emits topic `task.updated` naming every field that moved, with `changes.depends_on` carrying the two
lists rather than two scalars. Moving `is_archived` emits `task.archive_changed` **instead of**
carrying it inside `task.updated`'s `changes`, and a request doing both emits both.

Errors: `404 not_found` — unknown task code, unknown `priority`, unknown `assignee`, or a
`depends_on` code naming a task that does not exist or was removed. `409 conflicting_state` — a
`depends_on` entry that would close a loop in the project's dependency graph, checked against the
graph **as it would stand after the replacement**. `422 validation_error` — an empty `title` or
`priority`, or a `depends_on` entry naming a task of another project.

### 2.8.3 `DELETE /api/v1/tasks/{code}` — remove a task

Authentication: any member. No body.

Response `204`, no content. The effect is `is_archived = true`, not a row deletion
([ADR 0012](adr/0012-soft-delete-for-tasks.md)): the task keeps its code forever, its dependency
edges still constrain the project's graph, and its notes and blockers survive. `PATCH` with
`{"is_archived": false}` (§2.8.2) puts it back.

**Idempotent.** Removing an already-removed task is the same `204`, with no second activity record
and no second event, so a retry after a dropped response is safe.

Emits `ActivityRecord(verb=TASK_REMOVED)` **on the project** — mirroring `TASK_ADDED`, because the
project timeline is where a task appearing and disappearing reads as one story — and topic
`task.archive_changed` with `is_archived: true`. Restoring writes `TASK_RESTORED` and the same
topic with `is_archived: false`.

Any member may remove a task, not only an ops lead: removal is reversible and fully audited, so
gating it would stop the person who created a task by mistake from undoing it.

What a removed task disappears from: the task list and its `count`, the project detail and its
`open_tasks` / `overdue_tasks` / `blocked_tasks`, the owner-load counts, the priority signals, and
the snapshot. What it deliberately does **not** free: its code, which is never reused; its
dependency edges, which still close cycles; and blockers raised against it, which stay open on the
project.

Errors: `404 not_found` — the code names no task at all.

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

#### 2.13.1 `GET /api/v1/activity` — portfolio-wide feed

The same table read across every project, task and blocker: "what moved today", which the
per-project timeline cannot answer. Items are the **identical** `ActivityOut`, so one client type
and one component render both surfaces; `entity` is what identifies a row's subject here.

Query: §1.3 pagination, plus

| Param | Type | Meaning |
| --- | --- | --- |
| `entity_type` | `project`\|`task`\|`blocker` | Only facts about that kind of thing. |
| `entity_id` | str | Business code (`PRJ-22`), meant to be paired with `entity_type`. |
| `verb` | str, repeatable | ORs its values. |
| `actor` | str | The alias stored on the record (`camila`, `system`). |
| `origin` | `MANUAL`\|`POLICY`\|`SYSTEM` | Who caused it: a person, the engine, the system. |
| `since` / `until` | datetime | Inclusive bounds on `occurred_at`. |
| `correlation_id` | uuid | Expands one decision, exactly as in 2.13. |

Ordered `-occurred_at, -id` only; there is no `order_by`, because a feed sorted by anything else is
not a narrative. Response `200: Paginated[ActivityOut]`, `count` being the total match, not the
page length.

**No facet is rejected for naming an unknown value.** An unrecognised `verb`, `entity_type`,
`origin` or `actor` returns an empty page, never `422`: those vocabularies move by migration and by
the accounts table, and a filter saved in someone's URL must not be able to break a read-only
screen. The only `422` on this endpoint is a malformed `correlation_id` or datetime.

`origin=POLICY` is how the engine's own behaviour is audited — every score the ranking moved on its
own, portfolio-wide, in one read.

### 2.14 `GET /api/v1/team/load` — the roster

Load is computed from `Task` rows at read time. The `Team` sheet counters in the source dataset
are a stale projection and are not imported.

Query — every parameter optional, and the filters that are columns of a person are applied by the
database while `overloaded` and `order_by` are applied to the derived rows:

| Parameter | Values | Default | Notes |
|---|---|---|---|
| `owner` | repeatable `User.code` | — | Restrict to these people |
| `role` | repeatable `Role.code` | — | ORs its values, like every multi-select |
| `q` | free text | — | Case-insensitive substring over `code` and the display name |
| `status` | `active` \| `inactive` \| `all` | `active` | Replaced the earlier `include_inactive` boolean, which could not express "only the people we have retired" |
| `overloaded` | `true` \| `false` | absent | Absent is everybody; `false` is **who has room**, which is the question somebody about to assign work is asking |
| `order_by` | see below, `-` prefix descends | `-utilization` | Outside the allowlist is `422` carrying `details.allowed` |

`order_by` allowlist: `label`, `role`, `utilization`, `load_points`, `capacity`, `open_tasks`,
`overdue_tasks`, `blocked_tasks`, `urgent_tasks`, `projects_owned`. Ties always break on `label`,
so two people on identical load keep a stable order between two reads of unchanged data.

Not paginated: the result set is bounded by how many people the company employs.

Response `200: { items: TeamLoadOut[] }`:

```
TeamLoadOut {
  alias, label: str
  role: str | null              # the label, to render
  role_code: str | null         # the slug, to send back to PATCH /team/members/{code}
  is_active: bool               # false = retired from new assignment; their work is untouched
  has_password: bool            # false = a real assignee who cannot sign in yet
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
  {"alias": "camila", "label": "Camila Torres", "role": "Delivery", "role_code": "delivery",
   "is_active": true, "has_password": true,
   "weekly_capacity_points": 40, "load_points": 62, "utilization": 1.55, "is_overloaded": true,
   "open_tasks": 28, "blocked_tasks": 7, "high_or_critical_open": 20, "overdue_tasks": 12,
   "projects_owned": 7},
  {"alias": "daniel", "label": "Daniel Rojas", "role": "Commercial / Delivery",
   "role_code": "commercial_delivery", "is_active": true, "has_password": true,
   "weekly_capacity_points": 40, "load_points": 24, "utilization": 0.6, "is_overloaded": false,
   "open_tasks": 11, "blocked_tasks": 2, "high_or_critical_open": 6, "overdue_tasks": 5,
   "projects_owned": 3}
]}
```

### 2.15 Roster writes — `/api/v1/team/members`

Reading the roster is everybody's business; changing it is an ops lead's. All four routes are
`auth=ops_lead` and answer `403 permission_denied` with `details.required = "ops_lead"` otherwise.
They live under `/team/` although the aggregate is `accounts.User`, because the URL space names
the product's surface and not the app that owns the row.

`MemberOut` is the identity half only — no load, because load is a portfolio aggregate and these
routes belong to identity:

```
MemberOut { alias, label: str, role: TaxonomyRef | null,
            weekly_capacity_points: int, is_active, is_ops_lead, has_password: bool }
```

| Route | Body | Success | Errors |
|---|---|---|---|
| `POST /team/members` | `{label, role?, weekly_capacity_points, password?}` | `201 MemberOut` | `422 validation_error` (unknown `role`, capacity out of 1–200, a password the validators refuse) |
| `PATCH /team/members/{code}` | `{label?, role?, weekly_capacity_points?, is_active?}` | `200 MemberOut` | `404 not_found`, `422 validation_error`, `403` |
| `DELETE /team/members/{code}` | — | `204` | `404 not_found`, `403` |
| `POST /team/members/{code}/password` | `{password}` | `200 MemberOut` | `404`, `422 validation_error`, `403` |

Four things are load-bearing:

* **`code` is derived, not typed, and it is permanent.** `POST` does not accept one — exactly as
  `POST /projects` does not — and the service mints it from `label` inside the creating
  transaction: `"Alejandro Vélez"` → `alejandro.velez`, accents stripped, first and last token,
  and `alejandro.velez2` for the second person of that name. It is the person's username, the
  `actor` of every activity record they cause and the `entity.id` of every event about them, so a
  typo typed once would be a typo the audit trail carries forever with no rename to fix it. The
  response carries the code that was assigned. A person whose name changed gets a new `label`,
  never a new code.
* **`DELETE` deletes nothing.** The effect is `is_active = false`: the tasks and projects that
  name them are untouched, and a row removed underneath those would either cascade the history
  away or break the foreign keys holding it. `PATCH` with `is_active: true` restores them.
  Idempotent — retiring somebody already retired is the same `204` and writes no second record.
* **Absent ≠ `null` on `PATCH`.** An absent `role` is left alone; an explicit `null` unclassifies
  the person.
* **Nobody may retire their own account.** `403 permission_denied` — it would end the caller's own
  session, and if they were the last ops lead it would lock the product for everybody.

`password` omitted on creation is normal: the person is assignable immediately and cannot sign in
until one is set. A refused password answers with **every** rule it broke in
`details.fields.password`, not the first — a form that fixes one rule per round trip is a form
people work around by choosing something worse. The password is never echoed, never logged and
never published: this route emits no event at all.

### 2.16 `POST /api/v1/catalog/roles`, `PATCH /api/v1/catalog/roles/{code}`

The one taxonomy writable from the product; the other five are decisions about how the business
works and are still made in `/admin/`. A role is what an operator needs *while doing something
else* — registering somebody who does a job nobody has typed yet — and a trip to the admin
mid-form is how a person gets filed under the wrong role permanently. Both routes are ops lead.

| Route | Body / query | Success | Errors |
|---|---|---|---|
| `GET /catalog/roles` | `status` ∈ `active \| inactive \| all` (default `all`) | `200 TaxonomyRef[]` | `403` |
| `POST /catalog/roles` | `{code, label}` | `201 TaxonomyRef` | `409 conflicting_state`, `422`, `403` |
| `PATCH /catalog/roles/{code}` | `{label?, is_active?}` | `200 TaxonomyRef` | `404 not_found`, `422`, `403` |

`GET /catalog/roles` is the **editor's** list and is deliberately not a flag on `GET /catalog`.
That one feeds the pickers, where a retired value must never appear; this one has to show the
retired rows, because a screen that offers "retire" and cannot show what was retired offers a
delete with better manners. It is ops lead for the same reason the writes are: which roles were
retired is not something a reader needs in order to read the roster.

A new role is active and goes last in the picker. `is_active: false` takes it out of the pickers
and leaves everybody already classified under it exactly as they are — nothing is deleted, so
nobody is silently unclassified. A taken code is a conflict **including a retired role's**: the row
still exists, so restore it rather than create a second one that would resolve ambiguously ever
after. Every change writes an `ActivityRecord` under `entity_type: "role"`; none emits an event,
because a role is picker vocabulary that nothing recomputes from.

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
  project_types, stages, priorities, roles: TaxonomyRef[]
  engagement_types: EngagementTypeRef[]
  currencies: CurrencyRef[]
}
CurrencyRef {                    # TaxonomyRef plus one field
  code: str                      # ISO-4217 alphabetic, upper case
  label: str
  color: str | null
  minor_units: int               # 0..4 — decimal places the amount is written with
}
EngagementTypeRef {              # TaxonomyRef plus one field
  code: str
  label: str
  color: str | null
  weight: str                    # decimal, > 0 — the ranking multiplier (ARCHITECTURE §4.1)
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

`weight` travels for the same kind of reason: it is what makes converting a project from one
engagement type to another move it in the queue, and the dialog that asks somebody to confirm that
has to be able to say so. The alternative is a map from `diagnostico` to `1.10` held in the
frontend — the business enum in code hard rule 1 forbids, and wrong the day an operator edits a
weight in the admin. It is a **string**, the way every decimal on this API travels; parse it,
do not read it as a number off the wire. It is the multiplier the weighted signal sum is
multiplied by, never an addend, so `1.00` is the neutral value and the database refuses `0`.

**Workflow states are deliberately absent.** A state code is unique only inside its workflow, and
the legal moves out of the state a project is actually in are `transitions` on the project detail
(§2.2). A global list of states would invite the client to guess legality, which is exactly what
that field exists to prevent. The *configured shape* of a workflow — which columns a board has and
which arrows an operator drew between them — is a different question and is served by §2.18.

### 2.18 `GET /api/v1/workflows` — every state graph as an operator configured it

Authenticated like every other read; the opt-out list in §1.2 is five routes long and closed.

Response `200: WorkflowCatalogOut`:

```
WorkflowCatalogOut {
  workflows: WorkflowShape[]
}
WorkflowShape {
  code: str                      # stable slug of the graph
  name: str                      # operator-editable display name
  applies_to: "PROJECT"|"TASK"   # kind of aggregate the graph governs
  is_default: bool               # the fallback graph for its applies_to
  is_active: bool                # false = retired; still served, see below
  engagement_types: TaxonomyRef[]
  states: WorkflowNode[]         # every node, in the operator's `order`
  transitions: WorkflowEdge[]    # every ACTIVE edge, grouped by source column
}
WorkflowNode {                   # a StateRef plus what shaping the graph needs
  code: str                      # unique inside THIS graph only
  label: str
  category: "BACKLOG"|"IN_PROGRESS"|"BLOCKED"|"DONE"|"CANCELLED"
  color: str | null
  order: int                     # the operator's arrangement, as a number
  is_initial: bool               # where new aggregates of this graph start
  is_terminal: bool              # the operator marked it as an end
  is_active: bool                # false = retired, still published (see below)
  record_count: int              # projects + tasks currently sitting on it
  can_retire: bool               # whether DELETE on it would succeed right now
}
WorkflowEdge {
  from_state: str                # WorkflowState.code of the source, inside this graph
  to_state: str                  # WorkflowState.code of the target, inside this graph
  label: str                     # the operator's wording — "Aprobar", "Pedir cambios"
  requires_reason: bool          # the move was configured to demand written text
  requires_fields: str[]         # aggregate attributes the move was configured to demand
}
```

```json
{"workflows": [{"code": "project_default", "name": "Ciclo de vida de proyecto",
                "applies_to": "PROJECT", "is_default": true, "is_active": true,
                "engagement_types": [],
                "states": [{"code": "descubrimiento", "label": "Descubrimiento",
                            "category": "BACKLOG", "color": "#f59e0b", "order": 1,
                            "is_initial": true, "is_terminal": false, "is_active": true,
                            "record_count": 4, "can_retire": false},
                           {"code": "bloqueado", "label": "Bloqueado",
                            "category": "BLOCKED", "color": "#ef4444", "order": 4,
                            "is_initial": false, "is_terminal": false, "is_active": true,
                            "record_count": 0, "can_retire": true}],
                "transitions": [{"from_state": "descubrimiento", "to_state": "ejecucion",
                                 "label": "Iniciar ejecucion", "requires_reason": false,
                                 "requires_fields": ["next_step"]},
                                {"from_state": "ejecucion", "to_state": "bloqueado",
                                 "label": "Marcar como bloqueado", "requires_reason": true,
                                 "requires_fields": []}]}]}
```

One document rather than a route per engagement type, for the same reason §2.17 is one document: a
board draws all of its columns at once. Asking per engagement type would also force the client to
know *which* type to ask about before its first request, which means reimplementing the binding
precedence `Workflow.objects.resolve` owns (`DATA_MODEL.md` §2).

**`transitions` here is configuration; `transitions` on a project or a task is permission.** This
route shipped without edges so that no client could compute legality locally and skip the
per-project and per-task list, and that rule is unchanged. What makes edges safe on *this* document
is the distinction between the two questions. This document answers "what did an operator
configure": it is a picture of the graph as it stands in the admin. A record's own `transitions`
(§2.2) answers "what may **this** record do **right now**", and the two legitimately disagree — a
configured edge is refused when the record is not sitting on its `from_state`, when its guard
rejects the move (`409 guard_rejected`, §1.5, exists precisely because a declared edge can be
denied), and when a field named in `requires_fields` is empty *on that row*. **An edge existing is
not a move being legal.** A client that draws an arrow and then acts on it without asking the record
meets the same `409 transition_not_allowed` carrying `details.allowed` it met before. Nothing in the
enforcement path reads this document: `validate_transition` re-reads the rows every time it decides.

**Nor is it the global state list §2.17 refuses.** States arrive grouped under the graph that owns
them — never flat, so the collision that makes a global list unsafe (`bloqueada` in two graphs)
cannot happen — and every edge names its endpoints by code *inside that same graph*. A board needs
the column set and cannot derive it: columns inferred from the states projects happen to occupy
cannot represent an empty one, so a workflow whose `Bloqueado` state is unoccupied has no such
column, cannot say "nothing is blocked", and cannot accept a card dropped into it. A lifecycle
diagram needs the arrows and cannot derive those either, since an edge no record has taken is
invisible in the data.

`transitions` carries **active edges only**. `is_active = false` is how an operator withdraws a
move; the transition service refuses it, so publishing it as a drawable arrow would advertise a move
nothing can take. An empty list is legitimate and means no move has been declared yet. The `guard`
name is deliberately **not** published, for the same reason it is absent from §2.2: whether a guard
passes depends on facts no row in the graph holds, so naming it would invite the client to predict
an answer it cannot compute. Endpoints are codes rather than `StateRef` objects because the same
document publishes every node in full under `states`; duplicating labels onto both ends of every
arrow is how one response ends up disagreeing with itself after a rename.

Unlike §2.17, **retired graphs are served too**, flagged `is_active: false`. The catalog filters
because it feeds pickers, where offering a retired value would let somebody choose it and quietly
un-retire it. Nothing here is chooseable, and aggregates keep sitting on the states of a retired
graph — they are `PROTECT`ed exactly so — so a board that could not draw their columns would lose
those projects from the screen entirely.

`engagement_types` lists the types whose active binding names this graph; empty is the common case
and means the graph is reached as the per-kind default rather than by name. `states` is empty for a
graph nobody has configured states for, which is an answer and not an error.

**A retired node is published, flagged `is_active: false`** — the opposite treatment from a
withdrawn edge, and the asymmetry is the argument. An edge is a *move*, so publishing one nothing
can take would be a lie; a node is a *place*, and a record may still be standing in it, so a board
that could not draw the column would lose those records from the screen. Clients must not offer a
retired node as a drop target or in a picker.

`record_count` and `can_retire` are what let this one document render an *editor* and not only a
board: `can_retire` is the server's answer to "would `DELETE` on this column succeed right now"
(it is in service and nothing occupies it), and `record_count` is the number an operator is shown
when it would not. Neither says anything about legality — which move a *record* may make is still
computed per record, in §2.2.

### 2.19 Authoring a lifecycle — `POST/PATCH/DELETE /api/v1/workflows/...`

**Every route here is ops lead only** (`User.is_ops_lead`, the same rule as §2.6 and §2.15), and
every one of them writes an `ActivityRecord` under `entity_type: "workflow"`, `entity_id` = the
graph's `code`. Reading (§2.18) stays open to any authenticated member: reading the shape of the
operation is not the same permission as deciding it. The Django admin remains a second door, not the
only one — a lifecycle reshapeable only by somebody holding an admin account is configurable by
engineering, not by the operation.

**Every write answers with the whole graph** — `200 WorkflowShape`, or `201 WorkflowShape` on a
create — in exactly the shape §2.18 publishes. An editor that added a state gets back the
arrangement including the `order` it did not send, the occupancy that decides which columns may now
be retired, and the arrows a retirement withdrew; none of that is derivable from an echo of the
request.

| Route | Body | Success | Errors |
|---|---|---|---|
| `POST /workflows` | `{code, name, applies_to, engagement_types?}` | `201` | `409 conflicting_state` (code taken; or an engagement type already bound — `details.workflow` names the graph holding it), `422 validation_error` (`applies_to` outside `PROJECT`\|`TASK`, unknown engagement type), `403` |
| `PATCH /workflows/{code}` | `{name?, is_active?}` | `200` | `404 not_found`, `422`, `403` |
| `POST /workflows/{code}/states` | `{code, label, category, color?, order?}` | `201` | `404 not_found` (no such graph), `409 conflicting_state` (state code taken in this graph, retired or not), `422` (category outside the five — `details.allowed` lists them), `403` |
| `PATCH /workflows/{code}/states/{state_code}` | `{label?, category?, color?, order?, is_active?}` | `200` | `404 not_found`, `422`, `403` |
| `DELETE /workflows/{code}/states/{state_code}` | — | `200` | `404 not_found`, `409 conflicting_state` (records occupy it), `403` |
| `POST /workflows/{code}/transitions` | `{from_state, to_state, label, requires_reason?, requires_fields?, guard?, order?}` | `201` | `404 not_found` (no such graph, or an endpoint is not a state **of this graph**), `409 conflicting_state` (the ordered pair already has an edge), `422` (unregistered guard), `403` |
| `PATCH /workflows/{code}/transitions/{from}/{to}` | `{label?, requires_reason?, requires_fields?, guard?, order?, is_active?}` | `200` | `404 not_found`, `422`, `403` |
| `DELETE /workflows/{code}/transitions/{from}/{to}` | — | `200` | `404 not_found`, `403` |
| `GET /workflows/guards` | — | `200 str[]` | `403` |

Seven things are load-bearing:

* **`DELETE` retires; it never deletes.** States are `PROTECT`ed by every project and task standing
  on them and edges are named by the trail and by every event that traversed them, so a row removed
  underneath either would turn a readable audit into codes nothing resolves. The effect is
  `is_active = false`, and `PATCH` with `is_active: true` is the way back. `is_active` on a `PATCH`
  body accepts **only `true`** — retiring can be refused by records the request never looked at, and
  a field that sometimes fails for reasons unrelated to itself is a field a form cannot explain.
* **Retiring an occupied state is refused, and the refusal counts what is in the way.**
  `409 conflicting_state` with
  `details: {entity, id, workflow, current: "occupied", records, projects, tasks}` — for example
  `"State 'ejecucion' of workflow 'project_default' still holds 14 project(s) and 0 task(s)."` A
  record parked on a column outside the graph would have nowhere to be drawn and no move to make, so
  the operator moves them first. `can_retire` in §2.18 is the same rule, answered before the click.
* **Retiring a state withdraws every arrow touching it**, in and out, in the same transaction and
  under the same `correlation_id`; the trail entry lists them in
  `metadata.withdrawn_transitions`. An arrow into a column that left the graph is a move nothing may
  take.
* **Withdrawing the last move *out of* a state is allowed.** That is how a terminal state is
  declared — nothing leaves `entregado`, so `entregado` is where the lifecycle ends. No record is
  harmed by losing a move it had not taken.
* **A graph stays coherent by construction.** Both endpoints of a transition are resolved *inside*
  the workflow in the path, so an edge across two lifecycles cannot be expressed — a state code is
  unique only inside its graph, and "a state of another graph" is answered `404`, the same as "no
  such state". `category` must be one of the five; `guard`, if given, must be one of
  `GET /workflows/guards`, checked at authoring time so a typo cannot sit in the graph claiming to
  enforce a check that evaporated.
* **`order` appends.** A state created without one goes after the last column of that graph, not to
  position `0` in front of an arrangement somebody already made. The same holds for an edge among
  the moves leaving its source. The first state of an empty graph becomes its entry node, because a
  graph authored entirely from the product could otherwise hold no new work.
* **Authoring never grants a move.** Nothing on these routes is read by the transition service,
  which re-reads the rows every time it decides. Declaring an edge makes a move *available* from the
  state it leaves; whether a given project or task may take it still depends on the row it sits on,
  the fields filled in on it and the guard, and still comes from §2.2. `guard` is accepted as input
  here and is published on **no** read — naming it would invite a client to predict an answer it
  cannot compute.

### 2.20 Which lifecycle one record follows — `PUT/DELETE /api/v1/{projects,tasks}/{code}/workflow`

**Any member for a project; ops lead for a task.** Every successful write appends an
`ActivityRecord` with verb `WORKFLOW_ASSIGNED` naming **both** graphs in `from_value`/`to_value`,
plus a `project.updated` / `task.updated` event through the outbox.

The project routes were ops-lead and are not any more. What made that untenable is that the same
act reaches the operation by a second, unguarded door: changing a project's `engagement_type`
through §2.4 re-resolves the binding and therefore the graph, and that field is any member's. One
act behind two doors, one locked and one open, is not a permission model — it is a lock on the door
nobody was using. Closing the other door instead would take the engagement type away from the
people who own it. **Authoring a graph (§2.19) is still ops lead**: shaping a lifecycle and putting
one record on an existing one are different acts, and only the first changes what everybody else
can do.

| Route | Body | Success | Errors |
|---|---|---|---|
| `PUT /projects/{code}/workflow` | `{workflow}` | `200 ProjectDetailOut` | `404 not_found` (no such project, or no such workflow), `409 conflicting_state`, `422 validation_error` |
| `DELETE /projects/{code}/workflow` | — | `200 ProjectDetailOut` | `404`, `409`, `422` |
| `PUT /tasks/{code}/workflow` | `{workflow}` | `200 TaskDetailOut` | `404 not_found` (no such task, or no such workflow), `409 conflicting_state`, `422 validation_error`, `403` |
| `DELETE /tasks/{code}/workflow` | — | `200 TaskDetailOut` | `404`, `409`, `422`, `403` |

`PUT` sets the assignment; `DELETE` removes it and the record inherits one again — its engagement
type's binding, the per-kind default binding, or the default graph, in that order (`DATA_MODEL` §2).
`DELETE` removes the *assignment*, never a workflow and never the record; the task-removal route is
`DELETE /tasks/{code}`, one path segment up.

Both details report the answer, and it is two facts rather than one:

```json
"workflow": {"code": "ciclo-corto", "name": "Ciclo corto", "source": "DIRECT"}
```

`source` is `DIRECT` when somebody chose this graph for this record and `INHERITED` when nobody
did. The UI needs the difference: only a `DIRECT` record has anything to revert. `code`/`name` are
always the graph that owns the state the record is standing on — the graph whose edges `transitions`
comes from — and never the binding re-evaluated at read time, which after a rebinding would name a
lifecycle the record is not on.

Five things are load-bearing:

* **It changes the graph, never the state.** The record keeps the state `code` it was standing on
  and is repointed at that same code inside the target graph. So this route cannot reach a state no
  edge leads to, `POST /{projects,tasks}/{code}/transition` remains the only way a record *moves*,
  and CLAUDE.md rule 2 is untouched.
* **An incompatible target is refused, and that is the whole decision.** When the target graph has
  no **active** state carrying the record's current state code, the answer is
  `409 conflicting_state` with
  `details: {entity: "workflow_state", id, record, workflow, current_workflow, current: "incompatible", available}` —
  for example `"PRJ-01 sits on state 'blocked', which workflow 'ciclo-corto' does not contain."`,
  `available: ["execution", "entregado"]`. The alternative considered and rejected was letting the
  caller name a landing state in the target: there is no edge from a node of one graph to a node of
  another — an edge joins two states of one graph, by construction — so a landing state could be
  validated against nothing, and the route would become an unrestricted `UPDATE workflow_state` over
  HTTP. The operator has two honest ways forward and `available` is what makes them visible: add the
  missing column to the target (§2.19), or move the record along its current graph to a state the
  target does contain, then reassign.
* **A refusal changes nothing.** No column is written, no `ActivityRecord`, no event. A record is
  never left standing on a state its own workflow does not contain.
* **Inheriting is checked like anything else.** `DELETE` resolves the target through the binding
  ladder and then applies the same compatibility rule, so a record cannot be dropped back onto a
  lifecycle with no column for where it stands.
* **The other refusals.** `422 validation_error` with `details.fields.workflow` when the named graph
  governs the other kind of aggregate — a project cannot follow a task lifecycle. `409` with
  `details.current: "retired"` when the target is out of service: retirement means "no more
  arrivals", while records already inside a retired graph stay and keep obeying it. A reassignment
  that changes nothing is `200` and writes nothing.

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
  `PriorityPolicy` version is activated; the UI renders whatever entries arrive — from each line's
  own `label` and `reason` — and must not hardcode the six current ones or map their codes to text.
- `RiskFlag.code` values as a closed set. New specifications add codes (`CLAUDE.md` rule 8); the
  UI renders unknown codes with their `label` and `reason` rather than dropping them.
- The wording of `label` and of `reason` strings and of `message`. They are generated text, not
  identifiers — render them, never branch on them.
- `metadata` on `ActivityRecord` — free-form JSONB, per-verb, and it evolves.
- `ProjectSnapshot`, the outbox table, the Celery queues and the handler registry. None of them are
  addressable over HTTP; the SSE endpoint is the only live window onto the bus, and
  `GET /api/v1/health/pipeline` is the only aggregate one. The dead-letter re-queue lives in the
  admin, deliberately: replaying an event is an operator action with a person behind it.
- Ordering of any list beyond the documented default and the `order_by` allowlist.
- The Django admin at `/admin/`. It is an operator tool, not an API.
