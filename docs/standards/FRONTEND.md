# Frontend code standards — TypeScript / Astro

Read this before writing a line of TypeScript or Astro in `frontend/`. It is the frontend
counterpart of the layering rules in `docs/ARCHITECTURE.md` §7 and §9. The wire contract is
`docs/API.md`; the visual system is `DESIGN.md`. Where this file and one of those disagree, fix
one of the two.

## 1. TypeScript configuration and typing

`frontend/tsconfig.json` extends `astro/tsconfigs/strict` and adds two flags:

```jsonc
{
  "extends": "astro/tsconfigs/strict",
  "compilerOptions": {
    "noUncheckedIndexedAccess": true,
    "exactOptionalPropertyTypes": true
  }
}
```

- `strict` — the baseline. Without `strictNullChecks`, `target_date: date | null` from the API
  collapses into `string` and the `NO_TARGET_DATE` case stops being visible to the compiler.
- `noUncheckedIndexedAccess` — every array and record index yields `T | undefined`. Half of this
  UI indexes into collections that are legitimately empty: `breakdown[0]` on a project with no
  computed score, `transitions[0]` on a terminal state, `items[0]` on a filtered queue. The flag
  turns each of those into a compile error instead of a runtime `undefined.reason`.
- `exactOptionalPropertyTypes` — `{ reason?: string }` stops accepting `reason: undefined`.
  A transition with `requires_reason: true` must carry a real string; passing an explicit
  `undefined` is exactly the bug this flag catches, and it is otherwise indistinguishable from
  "not provided".

No `any`. No `as` to silence the compiler. No non-null assertion (`!`) — narrow instead.

```ts
// WRONG — asserts away the flag that exists to protect this line
const top = queue.items[0]!;
render(top.score.value);

// RIGHT — the empty queue is a view state, not an impossibility
const top = queue.items[0];
if (top === undefined) return { kind: "empty" } as const;
render(top.score.value);
```

`unknown` is allowed at exactly one boundary: the JSON parsed out of an SSE `data:` line, which is
narrowed by a parser before it reaches any store or component.

### Generated API types

Response and request types are generated from the backend OpenAPI schema. They are never
hand-written, so they cannot drift from `backend/apps/*/api/schemas.py`.

```bash
npm run gen:api
# → npx openapi-typescript http://localhost:8000/api/v1/openapi.json -o src/lib/api/types.ts
```

`frontend/src/lib/api/types.ts` is generated output: committed, never edited. A field you need but
cannot find is a backend schema change, not a local `interface`. Domain aliases are derived from
the generated tree, not retyped:

```ts
import type { components } from "./types";

export type QueueItem = components["schemas"]["QueueItemOut"];
export type Transition = components["schemas"]["Transition"];
export type RiskFlag = components["schemas"]["RiskFlag"];
```

### Discriminated unions for view state

Three independent booleans admit states that cannot exist and silently drop the ones that must be
handled — `ARCHITECTURE.md` §9 makes loading, empty, error and SSE-disconnected mandatory on every
view.

```ts
// WRONG — isLoading && isError && data is representable; "disconnected" has nowhere to live
interface QueueState {
  isLoading: boolean;
  isError: boolean;
  data: QueueItem[] | null;
}
```

```ts
// RIGHT — one shape at a time, and the compiler enumerates them
export type QueueView =
  | { kind: "loading" }
  | { kind: "ready"; items: QueueItem[]; lastEventId: string | null }
  | { kind: "empty"; appliedFilters: number }
  | { kind: "error"; code: ApiErrorCode; message: string }
  | { kind: "disconnected"; items: QueueItem[]; retryInSeconds: number };
```

`disconnected` carries the last known `items` on purpose: the board keeps rendering stale rows
with a visible staleness marker rather than blanking. A `switch (view.kind)` with no `default` and
a `never` fallthrough makes an unhandled state a build failure.

## 2. Documentation

JSDoc on every exported function, every store, and every non-obvious component prop. Write the
invariant and the failure mode. Restating the type is noise the compiler already covers.

```ts
// WRONG
/** Connects to the stream. Returns an unsubscribe function. */
export function subscribe(topic: Topic, handler: Handler): () => void;
```

```ts
/**
 * Subscribes a handler to one SSE topic on the shared connection.
 *
 * The first subscriber opens the connection; the last unsubscribe closes it. The store
 * deduplicates on envelope `id`, so a handler runs once per event even though delivery is
 * at-least-once (`docs/API.md` §3.5).
 *
 * Failing to call the returned cleanup leaks the handler across Astro view transitions: the
 * detached island keeps patching rows that are no longer in the DOM and the connection is
 * never released.
 */
export function subscribe(topic: Topic, handler: Handler): () => void;
```

Astro components declare props through an exported `interface Props`, documented per field where
the field is not self-explanatory.

```astro
---
/**
 * One row of the command center status matrix.
 */
export interface Props {
  item: QueueItem;
  /** Fixed legend order; every flag renders lit or unlit (DESIGN.md, The Ghost Legend Rule). */
  legendOrder: readonly RiskFlagCode[];
  /** Row is patched live by the SSE store. False for the server-rendered print view. */
  live?: boolean;
}
const { item, legendOrder, live = false } = Astro.props;
---
```

## 3. Component standards and Clean Code

One component, one responsibility. `ProjectRow` renders a row. It does not fetch the queue, does
not compute a score, does not decide which transitions are legal.

Presentational components receive data and emit intent. They never call the API client and never
touch the store. Fetching happens in the page (`frontend/src/pages/`) for the first paint, and in
the island's container component for live patches.

Props are explicit. Passing a whole `QueueItem` into a component that reads three fields hides the
dependency and forces every caller to construct the full entity.

```astro
---
// WRONG — takes the entity, reads three fields, and now cannot be tested or reused
export interface Props { project: QueueItem }
const { project } = Astro.props;
---
<span>{project.owner.label} · {project.open_blockers} · {project.state.label}</span>
```

```astro
---
// RIGHT — the contract is the data actually used
export interface Props {
  ownerLabel: string;
  openBlockers: number;
  stateLabel: string;
}
const { ownerLabel, openBlockers, stateLabel } = Astro.props;
---
<span>{ownerLabel} · {openBlockers} · {stateLabel}</span>
```

No logic in the template beyond a single ternary. Anything longer becomes a named value above the
markup.

```astro
---
// WRONG — the deadline rule is now unreadable and untestable
---
<td>{item.target_date === null ? "NO DATE" : (daysUntil(item.target_date) < 0 ? `+${Math.abs(daysUntil(item.target_date))}d OVERRUN` : `${daysUntil(item.target_date)}d`)}</td>
```

```astro
---
import { formatCountdown } from "../lib/format/countdown";
const countdown = formatCountdown(item.target_date, Astro.locals.now);
---
<td class="figure">{countdown.text}</td>
```

Naming:

- Components are nouns: `ProjectRow`, `ScoreBreakdown`, `BlockerPanel`, `TransitionBar`,
  `StreamStatus`. Never `HandleProjects`, never `DoTransition`.
- Handlers are `onX` as a prop, `handleX` as the implementation: `onTransitionRequested` /
  `handleTransitionRequested`.
- Booleans read as predicates: `isBlocked`, `isOverdue`, `hasNextStep`, `hasOpenBlockers`,
  `canTransition`. Not `blocked`, not `nextStep` for a boolean.
- No default exports for components or modules. Named exports only, so a symbol has one name
  across the codebase and rename refactors are mechanical.

File and directory layout under `frontend/src`:

```
src/pages/            route files, kebab-case, Astro's own convention: index.astro, projects/[code].astro
src/components/       PascalCase.astro / PascalCase.ts, one component per file, filename = export name
src/components/queue/ grouped by view, lowercase directory names
src/lib/api/          client.ts, types.ts (generated), errors.ts
src/lib/stream/       store.ts, topics.ts, backoff.ts
src/lib/format/       pure formatters: countdown.ts, score.ts
src/styles/           tokens.css, base.css
```

Everything under `src/lib/` is framework-free TypeScript with no Astro import, for the same reason
`backend/apps/*/domain/` has no Django import: it must be testable in isolation.

## 4. Islands and hydration

The default is zero JavaScript. A component ships as server-rendered HTML unless it needs
interaction or live data. Adding a `client:*` directive is a decision that must be justifiable in
one sentence.

| Directive | Use it for | In this app |
|---|---|---|
| `client:load` | Interactive immediately on first paint, above the fold | The status matrix rows and `StreamStatus` — the operator reads the board in the first seconds and it must already be live |
| `client:idle` | Interactive, but not in the first seconds | The queue filter bar, the transition bar on the project detail |
| `client:visible` | Below the fold, cost only if scrolled to | The activity timeline on `/projects/{code}` |
| `client:only` | Cannot be server-rendered because it depends on browser-only state | Nothing today. Reach for it only when SSR is genuinely impossible, and accept that it has no first paint |

Concretely: on `/`, the live rows of the command center hydrate. The summary panels — owner load
totals, portfolio counters, the legend key — do not; they are server-rendered and re-rendered on
navigation.

The smell: an island that hydrates only to render static text. If the component has no event
handler and no store subscription, its `client:*` directive is dead weight that ships a runtime for
markup the server already produced. Delete the directive, not the component.

## 5. SOLID applied to the frontend

**SRP — the store owns the connection.** `frontend/src/lib/stream/store.ts` is the only module
that constructs an `EventSource`. A component that opens its own gets a second connection, a
second `Last-Event-ID` cursor, and duplicate patches.

```ts
// WRONG, inside a component
const es = new EventSource("/api/stream");
es.addEventListener("project.state_changed", patchRow);
```

```ts
// RIGHT
import { subscribe } from "../lib/stream/store";
const off = subscribe("project.state_changed", patchRow);
// call off() on cleanup
```

**OCP — transitions come from the API.** `docs/API.md` §2.2: `transitions` is the only source of
transition buttons. Adding a workflow state is a fixture change (`CONTRIBUTING.md` §8) and must
require no frontend edit.

```ts
// WRONG — a new state means editing this switch, and an illegal move is now reachable
switch (project.state.code) {
  case "ejecucion": return ["bloqueado", "en_revision", "pausado"];
  case "bloqueado": return ["ejecucion"];
}
```

```astro
---
export interface Props { transitions: readonly Transition[]; missingFields: readonly string[] }
const { transitions, missingFields } = Astro.props;
---
{transitions.map((t) => (
  <TransitionButton
    label={t.label}
    toStateCode={t.to_state.code}
    stateCategory={t.to_state.category}
    requiresReason={t.requires_reason}
    blockedByFields={t.requires_fields.filter((f) => missingFields.includes(f))}
  />
))}
```

**LSP — one contract per risk flag.** All six flags (`BLOCKED`, `OVERDUE`, `NO_NEXT_STEP`,
`NO_TARGET_DATE`, `STALE`, `OWNER_OVERLOADED`) render through one `RiskLegend` component taking
`{ code, severity, reason, lit }`. No flag gets a special-cased component, and no caller branches
on `code` to decide which component to mount. A seventh flag added on the backend appears in the
legend block with no new component.

**ISP — narrowest props.** `ScoreBreakdown` takes `readonly BreakdownEntry[]`, not `QueueItem`.
`OwnerLoadCell` takes `{ alias, label, loadPoints, capacityPoints }`, not `User`. A props
interface that names the entity is usually asking for too much.

**DIP — depend on the client and the store.** Components import `apiClient` from
`frontend/src/lib/api/client.ts` and `subscribe` from `frontend/src/lib/stream/store.ts`. A raw
`fetch(` or `new EventSource(` anywhere outside those two modules is a review rejection: it
bypasses the `Authorization: Bearer` header and its silent refresh — for the stream, the
`withCredentials` cookie authentication — the typed error envelope (`code`, never `message`), and
the shared connection.

## 6. State and data flow

One `EventSource` per browser tab, owned by the store. Several connections mean the server fans the
same events into each of them, each holds an independent `Last-Event-ID`, and a reconnect replays
into one while the others sit ahead — rows then disagree inside a single page. The store also owns
deduplication on envelope `id`, which only works if every event passes through it once.

Subscription and cleanup:

```ts
const off = subscribe("project.priority.recalculated", (event) => patchScore(event));
window.addEventListener("beforeunload", off);
// and on island teardown / Astro view transition, call off()
```

Refcounting is the store's job: the first `subscribe` opens the connection, the last `off` closes
it. A subscription that is never released keeps a detached DOM node alive and holds the connection
open on a page that no longer displays it.

First paint is server-rendered from `GET /api/v1/...`; live patches arrive over SSE. The two must
not disagree:

- The server-rendered HTML carries the response's `updated_at` and the store's starting
  `last_event_id` in a data attribute; the island hydrates from the DOM instead of refetching.
- A patch older than the rendered `updated_at` is discarded, not applied.
- On `event: stream.reset` with `last_event_id_expired`, the store refetches the affected resource
  rather than trusting local state (`docs/API.md` §3.5).
- Patches are field-level against the rendered row. Never rebuild the whole matrix from an event
  payload — the payload carries the change, not the entity.

Reconnection uses exponential backoff with jitter in `frontend/src/lib/stream/backoff.ts`, floored
by the server's `retry: 3000`, and is surfaced: the `disconnected` view state shows the retry
countdown and a manual retry control. A manual retry builds a new `EventSource` and must pass
`?last_event_id=` explicitly, because a constructed connection does not carry the header. Silent
reconnection is forbidden — an operator reading a stale board must know it is stale.

## 7. Styling

Design tokens from `DESIGN.md` live once, as CSS custom properties in
`frontend/src/styles/tokens.css`, and are the only place a literal colour, font or spacing value
appears.

```css
:root {
  --color-panel: #2e3532;
  --color-plate: #141917;
  --color-go: #63a17a;
  --color-caution: #d9a441;
  --color-nogo: #c4483c;
  --color-legend: #e8e4d9;
  --color-legend-dim: #8a928d;
  --color-rule: #414a46;
  --space-1: 4px;
  --radius: 0;
}
```

Every view implements the four mandatory states — loading, empty, error, SSE-disconnected — as
real markup, styled, not as an unstyled fallback string. A view with only a happy path is
incomplete, not "to be polished later".

No magic colour values in a component. A status colour comes from the state's own `category` (or
the flag's `severity`), never from a hex in a template:

```astro
<!-- WRONG — hardcodes a hex, and a workflow state added in the admin renders unstyled -->
<span style="color:#C4483C">{item.state.label}</span>
```

```astro
<!-- RIGHT — the category selects the token; new states inherit styling for free -->
<span class="state" data-category={item.state.category}>{item.state.label}</span>

<style>
  .state[data-category="BLOCKED"] { color: var(--color-nogo); }
  .state[data-category="IN_PROGRESS"] { color: var(--color-go); }
  .state[data-category="BACKLOG"] { color: var(--color-legend-dim); }
</style>
```

The API also returns `state.color` for taxonomies the operation recolours in the admin; bind it as
a CSS variable on the element rather than into a class name. Semantic green/amber/red keep their
single meanings (`DESIGN.md`, The Three Meanings Rule).

## 8. Accessibility and semantics

These are standards, checked in review, not a later pass.

- Real elements. The status matrix is a `<table>` with `<th scope="col">`, not nested `<div>`s. A
  transition control is a `<button>`; an illegal transition is a `<button disabled>` with
  `aria-describedby` naming the missing field, because `DESIGN.md` requires illegal transitions to
  render visibly dead rather than disappear.
- Focus is always visible. `outline: none` without a replacement indicator is a defect. The
  indicator is a border or background shift — never a shadow (The Inset-Not-Raised Rule).
- The status matrix is fully keyboard-reachable: every row's expand control and every legend cell
  with a reason is focusable in DOM order, and the expanded score breakdown is reachable without a
  pointer. Row expansion toggles `aria-expanded`.
- Events arriving over SSE are announced. The connection banner is `role="status"`
  (`aria-live="polite"`); a row patched live sets `aria-live="polite"` on the cell that changed, not
  on the whole table — announcing twenty-two rows on every recalculation is worse than announcing
  nothing.
- Every risk legend cell has a text alternative: colour alone never carries a meaning, and the
  unlit state is conveyed by `aria-disabled` plus its label, not only by Legend Dim.
- Spanish user-facing labels come from the API. Set `lang="es"` on the elements rendering them
  while the document is `lang="en"`, so a screen reader does not read Spanish with English
  phonetics.

## 9. The mechanical gate

Setup, through the CLIs (`CLAUDE.md`, dependencies are never hand-written into `package.json`):

```bash
npm create astro@latest                 # scaffold, once
npx astro add ...                       # any Astro integration
npm install --save-dev eslint typescript-eslint eslint-plugin-astro \
  eslint-plugin-jsx-a11y prettier prettier-plugin-astro
npm install --save-dev openapi-typescript
```

Commands that must pass before a pull request:

```bash
npx astro check          # TypeScript across .astro and .ts, with the strict flags above
npx eslint .
npx prettier --check .
npm run gen:api && git diff --exit-code src/lib/api/types.ts   # generated types are current
```

Enforced by tooling — a violation fails the build, so do not spend review comments on it:
`strict`, `noUncheckedIndexedAccess`, `exactOptionalPropertyTypes`, no `any`
(`@typescript-eslint/no-explicit-any`), no non-null assertion
(`@typescript-eslint/no-non-null-assertion`), no default export
(`import/no-default-export`, with `src/pages/**` exempted since Astro requires it), exhaustive
`switch` over view-state unions (`@typescript-eslint/switch-exhaustiveness-check`), unused
variables, formatting, and the a11y rules from `eslint-plugin-astro` / `jsx-a11y`. A lint rule
banning `new EventSource(` and bare `fetch(` outside `src/lib/stream/` and `src/lib/api/` is part
of this set.

Enforced by review — no tool sees these:

- Whether the hydration directive is justified, and whether an island exists only to render static
  text.
- Whether props are the narrowest data needed rather than a whole entity.
- Whether a component branches on a state `code` where it should be driven by the API's
  `transitions` or by `category`.
- Whether all four view states are implemented, and whether the disconnected state actually
  surfaces the retry.
- Whether the JSDoc states an invariant and a failure mode, or restates the type.
- Whether server-rendered content and the live patch can disagree.
