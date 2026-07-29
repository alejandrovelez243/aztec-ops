# Frontend design patterns

The catalog of patterns the Astro frontend uses, why each one exists **here**, and how to tell it
has been misused. Normative spec: `docs/ARCHITECTURE.md` §9. The HTTP and SSE contract is
`docs/API.md`. The visual system is `DESIGN.md`. Layout and hydration conventions are in
`.agents/skills/astro-sse-client/`.

Two views only: `/` (command center) and `/projects/{code}` (detail). Every pattern below exists to
keep those two views correct while state changes arrive from the bus.

```
frontend/src/
  pages/         index.astro, projects/[code].astro     # fetch on the server, compose
  layouts/       Shell.astro                            # slots, connect() called once
  components/    ScoreBreakdown.astro RiskFlags.astro Timeline.astro OwnerLoad.astro
  islands/       PriorityQueue.astro ProjectHeader.astro BlockerPanel.astro ConnectionBadge.astro
  lib/
    api/         client.ts  types.ts                    # types.ts generated, never hand-edited
    stream/      store.ts   topics.ts
    view-state.ts
  styles/        tokens.css
```

---

## 1. Islands Architecture

**Problem.** The command center is 22 rows of dense matrix that must paint instantly and then keep
ticking as `project.priority.recalculated` and the write-side topics arrive. Shipping the whole
board as client JS pays hydration cost for a score breakdown that never changes after first paint.

**Where.** `frontend/src/islands/` holds the live regions; `frontend/src/components/` holds
server-only `.astro` components with no colocated `<script>`.

**The decision rule.** A component becomes an island only if it re-renders from an SSE envelope or
takes user input. Everything else is static HTML the parent island patches.

| Component | Directive | Why |
|---|---|---|
| `PriorityQueue`, `ProjectHeader`, `ConnectionBadge` | `client:load` | live, above the fold |
| `BlockerPanel` on the command center | `client:visible` | live, below the fold |
| `ScoreBreakdown`, `RiskFlags`, `Timeline`, task table | none | patched by the parent island |

```astro
<!-- WRONG — pages/projects/[code].astro -->
<Timeline client:load records={project.activity} />
```

`Timeline` has no handler and no input. `client:load` ships a runtime for a list that only grows
when `ProjectHeader` receives `note.added` and appends a row.

```astro
<!-- pages/projects/[code].astro -->
<ProjectHeader client:load project={project} />
<Timeline records={project.activity} />   <!-- static; the header patches it -->
```

**Smell.** A `client:*` directive on a component whose module has no `subscribe(...)` call and no
event listener. Also: an island rendering more than one live region, so a `blocker.raised` event
re-renders the score plate too.

---

## 2. Observable Store / Publish-Subscribe

**Problem.** Four islands care about the stream. Four `EventSource` objects would open four
connections to `/api/stream`, make the backend fan the same envelopes out four times, and burn the
browser's per-origin connection budget on one tab.

**Where.** `frontend/src/lib/stream/store.ts`, topic constants in `frontend/src/lib/stream/topics.ts`.

**Contract.** `connect()` (idempotent, called once by `Shell.astro`), `subscribe(topic, handler) →
unsubscribe`, `onStatus(handler) → unsubscribe`, `retry()`. Handlers receive the parsed envelope
(`id`, `topic`, `occurred_at`, `actor`, `correlation_id`, `entity`, `payload`, `version`).

```ts
// WRONG — islands/BlockerPanel.astro <script>
const es = new EventSource("/api/stream");
es.addEventListener("blocker.raised", (e) => append(JSON.parse(e.data)));
// second connection, no dedupe, no last_event_id on reconnect, never closed
```

```ts
// islands/BlockerPanel.astro <script>
import { subscribe } from "../lib/stream/store";
import { BLOCKER_RAISED, BLOCKER_RESOLVED } from "../lib/stream/topics";

const off = [
  subscribe(BLOCKER_RAISED, (ev) => upsertBlocker(ev.entity.id, ev.payload)),
  subscribe(BLOCKER_RESOLVED, (ev) => removeBlocker(ev.payload.blocker_id)),
];
document.addEventListener("astro:before-swap", () => off.forEach((f) => f()));
```

**Rules for extending.** A new topic is a constant in `topics.ts` plus the fan-out allowlist
(`docs/API.md` §3.6) — never a string literal at the call site. Every `subscribe` return value is
kept and called; an island that mounts twice must not accumulate handlers. Handlers are safe to run
twice with the same `event.id`: delivery is at-least-once and the store deduplicates on id, but a
handler that appends to a list still needs to key on the entity code.

**Smell.** `new EventSource` outside `stream/store.ts`. `connect()` called from an island. A
`subscribe` whose return value is discarded. A handler that assumes ordering between
`project.state_changed` and `project.priority.recalculated`.

---

## 3. Adapter / API Client

**Problem.** The backend answers with a typed error envelope (`{code, message, details}`) and every
route needs the bearer token. Scattering `fetch` loses both, and loses the generated types.

**Where.** `frontend/src/lib/api/client.ts` wraps `frontend/src/lib/api/types.ts`, which is
generated from Ninja's schema and never hand-edited:

```bash
npx openapi-typescript http://localhost:8000/api/openapi.json -o frontend/src/lib/api/types.ts
```

The client returns a `Result`, it does not throw, because §4's union is driven by it.

```ts
// WRONG — islands/ProjectHeader.astro
const p = await (await fetch(`/api/v1/projects/${code}`)).json();
// no types, no Authorization header, a 409 transition_not_allowed parses as a success body
```

```ts
// lib/api/client.ts
export type Result<T> = { ok: true; data: T } | { ok: false; error: ApiError };
export type ApiError = { code: ApiErrorCode; message: string; details?: unknown };

// call site
const res = await api.transitionProject(code, { to_state, reason });
if (!res.ok && res.error.code === "transition_not_allowed") {
  await refetchProject(code);   // our view was stale; do not guess
}
```

**Rules for extending.** A new endpoint gets a method on the client typed from `types.ts` —
regenerate the types in the same change. New backend error codes are added to `ApiErrorCode` and
branched on by `code`, never by matching `message`, which is generated prose (`docs/API.md` §4.2).

**Smell.** `fetch(` anywhere under `pages/`, `components/` or `islands/`. A hand-written interface
duplicating a shape that already exists in `types.ts`. `try/catch` around a client call, which means
someone made it throw.

---

## 4. State machine for view state

**Problem.** Independent booleans (`loading`, `error`, `empty`, `offline`) make sixteen combinations
for five real ones. `loading && error` renders a skeleton over a failure; `!loading && !error &&
items.length === 0` silently renders nothing when the request failed and returned `[]`.

**Where.** `frontend/src/lib/view-state.ts` holds the union and the mapper from an api `Result` plus
the store status onto it.

```ts
// WRONG
let loading = true, error = null, items = [];
if (loading) renderSkeleton();
if (error) renderError(error);
if (!loading && items.length === 0) renderEmpty();   // also true while loading
```

```ts
// lib/view-state.ts
export type ViewState<T> =
  | { kind: "loading" }
  | { kind: "ready"; data: T; stale: false }
  | { kind: "empty" }
  | { kind: "error"; error: ApiError }
  | { kind: "disconnected"; data: T; lastEventAt: string };

export function fromResult<T>(
  res: Result<T[]>, status: StreamStatus, lastEventAt: string | null,
): ViewState<T[]> { /* one place decides; callers switch exhaustively */ }
```

**Rules for extending.** Every island and every view renders all five arms, switched exhaustively
(a `default: assertNever(state)`). `disconnected` carries the data — content stays on screen, marked
stale with the timestamp of the last event and a visible reconnect wired to `store.retry()`.
`error` and `disconnected` are different arms because they need different affordances: one retries a
request, the other retries a connection.

**Smell.** A boolean named `isLoading` next to a boolean named `hasError`. A `switch` without an
exhaustiveness check. `disconnected` collapsed into `error`, or the screen blanked while the stream
is down. An empty state that is a blank region instead of the sentence naming what is empty
("No open blockers").

---

## 5. Container / Presentational split

**Problem.** A component that fetches and renders cannot be rendered from server data on one page
and patched from an event on another, and it cannot be tested without a network.

**Where.** Containers are `pages/*.astro` frontmatter (server fetch) and the islands (event
patching). Presentational components are everything in `components/`: props in, DOM out, no
`client:*`, no import of `lib/api`.

```astro
---
// WRONG — components/ScoreBreakdown.astro
import { api } from "../lib/api/client";
const res = await api.getProject(Astro.props.code);   // second request for data the page has
---
```

```astro
---
// pages/projects/[code].astro — the container
import { api } from "../../lib/api/client";
const res = await api.getProject(Astro.params.code!);
if (!res.ok) return Astro.rewrite("/500");
const project = res.data;
---
<ProjectHeader client:load project={project} />
<ScoreBreakdown signals={project.score.breakdown} modifiers={project.score.modifiers} />
```

**Rules for extending.** A presentational component imports nothing from `lib/api` or `lib/stream`.
If it needs a value it does not have, the prop goes up to the container — it does not fetch. An
island is a container: it owns state and patches presentational DOM, it does not itself contain the
markup for a table, a timeline and a breakdown.

**Smell.** An import of `client.ts` inside `components/`. A component taking a `code` prop and
resolving the object itself. An island file over roughly 150 lines, which usually means it absorbed
markup that belongs in `components/`.

---

## 6. Server-side render plus live patch

**Problem.** First paint must be immediate and correct without JS, but the board must then track the
bus. Fetching on mount gives a flash and duplicates the request the server already made.

**Where.** Page frontmatter fetches for first paint; the island hydrates over the already-correct
DOM and only patches from then on.

```ts
// WRONG — islands/PriorityQueue.astro
onMount(async () => { rows = await api.getQueue(); });   // refetches what SSR already rendered
```

```ts
// islands/PriorityQueue.astro <script>
// The server rendered the rows. Read them, do not refetch.
subscribe(PROJECT_PRIORITY_RECALCULATED, (ev) => {
  const row = rows.get(ev.entity.id);
  if (!row) return;                                  // not on this board; ignore
  if (ev.occurred_at <= row.dataset.updatedAt) return;  // older than what we show
  patchScore(row, ev.payload.to);
  row.dataset.updatedAt = ev.occurred_at;
});
```

**Reconciliation rule when server state and the stream disagree.** The stream wins only if it is
newer: compare the envelope's `occurred_at` against the `updated_at` the row was rendered with, and
drop older envelopes. Two cases refetch instead of patching: a `stream.reset` frame
(`last_event_id_expired`, `docs/API.md` §3.5) and an `ApiError` of `transition_not_allowed`, both of
which mean the local view is not merely behind but wrong. Patching is never the way to recover from
a disagreement about *shape* — only about *value*.

**Smell.** An island fetching its own initial data. A patch applied without comparing `occurred_at`.
A `stream.reset` frame that is logged and ignored. A full page reload used as the reconciliation
strategy, which loses scroll position on a board built for a six-minute morning read.

---

## 7. Data-driven rendering

**Problem.** `CLAUDE.md` rule 1 and rule 8 promise that a new workflow state or a new risk criterion
is data plus one registry line. That guarantee dies the moment the frontend holds a list of state
codes. This section is the frontend half of that guarantee.

**Where.** `components/RiskFlags.astro`, `components/ScoreBreakdown.astro`, and the transition
control inside `islands/ProjectHeader.astro`.

```astro
---
// WRONG — components/RiskFlags.astro
const COLORS = { BLOCKED: "#C4483C", OVERDUE: "#C4483C", NO_TARGET_DATE: "#D9A441" };
const LABELS = { BLOCKED: "Bloqueado", OVERDUE: "Vencido" };
// a new specification ships a code this map does not have: the flag disappears
---
```

```astro
---
// components/RiskFlags.astro
const { flags } = Astro.props;   // RiskFlag[] = {code, severity, reason}
---
{flags.map((f) => (
  <span class="legend" data-severity={f.severity} title={f.reason}>{f.code}</span>
))}
```

**Rules for extending.** Colour comes from `severity` mapped to the three semantic tokens, never
from `code`. Labels and colours of states, priorities and taxonomies arrive as `{code, label,
color}` and are rendered, not translated. Transition buttons are rendered from
`project.transitions` — the complete and only set of legal moves — with `transition.label` as the
text. An unknown `RiskFlag.code` renders with its `reason`; it is never dropped. `ScoreBreakdown`
iterates whatever entries `breakdown` contains, in the order the API sent them (descending
`contribution`), and never assumes the current six signals.

**Smell.** Any object literal in `frontend/src` keyed by a state code, a risk code, a signal code or
a priority code. A `switch` on `state.code` — the only legal comparison is on `state.category`.
`grep -rn "'BLOCKED'\|\"ejecucion\"" frontend/src` returning anything outside a test fixture.

---

## 8. Optimistic UI — where it is used and where it is banned

**Problem.** Optimism is a bet that the server will agree. For a workflow transition the server is
the only thing that knows whether the move is legal: legality lives in `WorkflowTransition` rows,
guards and `requires_fields`. Painting the new state before the POST returns invents authority the
client does not have, and a rollback after the operator has already moved on is worse than a wait.

**Banned.** Workflow transitions, priority overrides, blocker resolution — anything that goes
through a service with a guard.

```ts
// WRONG — islands/ProjectHeader.astro
setState(toState);                                     // painted as done
await api.transitionProject(code, { to_state: toState });   // may return 409
```

```ts
// button enters "pending", nothing else changes
btn.dataset.pending = "true";
const res = await api.transitionProject(code, { to_state, reason });
btn.dataset.pending = "false";
if (!res.ok) return showError(res.error);
// the authoritative paint arrives as project.state_changed on the stream
```

**Allowed.** Purely local, server-independent interactions: expanding a row to reveal its score
breakdown, sorting a column the client already holds, opening the reason dialog, focus and filter
state. These have no server truth to contradict them.

**Rules for extending.** Before making anything optimistic, ask whether the server can refuse it.
If it can, the answer is a pending state plus the stream. A button disabled because a
`requires_fields` entry is empty names the missing field next to it rather than letting the operator
discover it via a 4xx.

**Smell.** A local `state` variable written before an `await` on a client call. A rollback path
(`revert()`, `previousState`) in an island — its existence means something was painted too early.
A transition button that becomes enabled again because the SSE event never arrived.

---

## 9. Renderless / slot composition

**Problem.** Both views need the same top plate, the same connection badge and exactly one
`connect()`. Copying that into two pages guarantees they drift, and duplicating `connect()` opens a
second stream.

**Where.** `frontend/src/layouts/Shell.astro`, composed with named slots. The layout carries
structure and the single connection; it knows nothing about queues or projects.

```astro
---
// layouts/Shell.astro
const { title } = Astro.props;
---
<header class="plate">
  <slot name="mission-clock" />
  <h1>{title}</h1>
  <ConnectionBadge client:load />   <!-- the only renderer of stream status -->
</header>
<main><slot /></main>
<slot name="board-footer" />
<script>import { connect } from "../lib/stream/store"; connect();</script>
```

```astro
<!-- pages/index.astro -->
<Shell title="Command center">
  <MissionClock slot="mission-clock" />
  <PriorityQueue client:load items={queue.items} />
</Shell>
```

**Rules for extending.** Shared chrome lives in the layout behind a named slot; a page never
re-implements the header. Every `<slot name>` has a fallback so a page that omits it still renders a
complete plate. `connect()` appears exactly once in the codebase.

**Smell.** A second `connect()` call. A layout importing `lib/api`. A page rendering its own header
because the slot did not fit — the fix is another slot, not a copy.

---

## 10. Design tokens as a single source of truth

**Problem.** `DESIGN.md` fixes three semantic colours with one meaning each, a 4px spacing rhythm,
zero radius and three type families. A hex literal in a component is a fourth source of truth that
no rule can enforce.

**Where.** `frontend/src/styles/tokens.css`, imported once by `Shell.astro`. It is the only file in
`frontend/src` containing a colour literal.

```css
/* WRONG — inside a component */
.row[data-health="blocked"] { color: #C4483C; border-radius: 2px; }
```

```css
/* styles/tokens.css — transcribed from DESIGN.md, names included */
:root {
  --console-green-grey: #2E3532;  --plotboard-black: #141917;
  --signal-green: #63A17A;        --caution-amber: #D9A441;  --no-go-red: #C4483C;
  --legend-ivory: #E8E4D9;        --legend-dim: #8A928D;     --rule-grey: #414A46;
  --sev-low: var(--signal-green); --sev-medium: var(--caution-amber);
  --sev-high: var(--no-go-red);
  --space-1: 4px; --space-2: 8px; --space-3: 12px; --space-4: 16px;
  --space-6: 24px; --space-8: 32px;
  --radius: 0;
  --font-legend: "Archivo Narrow", Arial Narrow, sans-serif;
  --font-body: Archivo, system-ui, sans-serif;
  --font-mono: "JetBrains Mono", ui-monospace, monospace;
}
```

**Rules for extending.** A new visual decision is added to `DESIGN.md` first, then to
`tokens.css`, then used. Severity maps to `--sev-*`; nothing maps a colour from an entity code.
Taxonomy `color` values from the API are data and may be used inline for a taxonomy chip — they are
the one exception, and they never override a semantic token. Every numeral in a repeated position
carries `font-variant-numeric: tabular-nums`.

**Smell.** A hex literal, an `rgb(`, a `px` spacing value off the 4px scale, or a non-zero
`border-radius` outside `tokens.css`. Any `box-shadow` that casts outward — depth here is inset, not
raised. A fourth semantic colour, or green/amber/red borrowed for a hover or a brand accent.

---

## Anti-patterns

Each of these is a review blocker, not a preference.

| Anti-pattern | Why it breaks this app | Fix |
|---|---|---|
| An `EventSource` per component | N connections per tab, N fan-outs per envelope, each with its own broken `Last-Event-ID` | `subscribe()` on `lib/stream/store.ts` (§2) |
| Prop drilling stream data through four levels | The leaf re-renders whenever an ancestor sees any event, and an event that skips the ancestor never reaches the leaf | The interested component subscribes directly (§2) |
| `useEffect`-style fetch on mount inside a presentational component | Duplicates the SSR request, flashes, and makes the component untestable without a network | Fetch in page frontmatter, pass props (§5, §6) |
| Hardcoded status colours or state codes | A new `WorkflowState` or risk specification is data; a frontend map silently drops it and breaks the open/closed guarantee | Render `label`/`color` from the API, branch only on `category` and `severity` (§7) |
| `client:load` on a static component | Ships a runtime for markup that only the parent island ever changes | Remove the directive; patch from the island (§1) |
| A global mutable object used as a store (`window.__aztec = {}`) | No subscription, no teardown, no status; writers and readers race and nothing re-renders | The store's `subscribe`/`onStatus` contract (§2) |
| A component that both fetches and renders complex markup | Cannot be server-rendered on one page and patched on another; two responsibilities, one file | Split container from presentational (§5) |
| Optimistically painting a workflow transition | The server owns legality; a 409 forces a rollback the operator has already read past | Pending state, then the `project.state_changed` envelope (§8) |
| Collapsing `disconnected` into `error`, or omitting either | Stale numbers shown as live is the one failure mode an operations board must not have | The five-arm union in `lib/view-state.ts` (§4) |
