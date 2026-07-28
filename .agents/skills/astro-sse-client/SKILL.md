---
name: astro-sse-client
description: Load when working in frontend/ — Astro pages, islands, hydration directives, the shared EventSource store in frontend/src/lib/stream/, reconnection with backoff and Last-Event-ID, the typed API client in frontend/src/lib/api/, transition buttons, or the loading/empty/error/disconnected view states.
---

# Astro frontend and the SSE client

Frontend for Aztec Ops. Two views only (ARCHITECTURE §9): `/` command center and
`/projects/{code}` detail. Server render everything; hydrate only the live regions.

## Layout

```
frontend/src/
  pages/
    index.astro              # command center
    projects/[code].astro    # detail
  components/                # .astro, server-only, no client script
    ScoreBreakdown.astro  RiskFlags.astro  OwnerLoad.astro  Timeline.astro
  islands/                   # live regions only, one colocated <script> each
    PriorityQueue.astro  ProjectHeader.astro  BlockerPanel.astro  ConnectionBadge.astro
  lib/
    api/client.ts  api/types.ts      # types.ts is generated, never hand-edited
    stream/store.ts  stream/topics.ts
    view-state.ts
```

Regenerate the API types from Ninja's schema, do not transcribe them by hand:

```bash
npx openapi-typescript http://localhost:8000/api/openapi.json -o frontend/src/lib/api/types.ts
```

## Hydration

A component gets client JS only if it re-renders from an SSE event or takes user input.
Everything else is a plain `.astro` component rendered once on the server.

- Live region above the fold (priority queue, project header, connection badge): `client:load`
  on a framework component, or a colocated `<script>` module in an `.astro` island.
- Live region below the fold (blocker panel on the command center): `client:visible`.
- Score breakdown, risk chips, timeline, task list on first paint: **no directive**. They are
  static HTML produced from the SSR fetch; when an event arrives, the parent island patches
  their DOM.

Data for first paint is fetched server-side in the page frontmatter. Islands never fetch on
mount to populate themselves — that duplicates the request and causes a flash.

## The single EventSource

One connection per browser tab, owned by `frontend/src/lib/stream/store.ts`. Islands import the
store and subscribe; they never construct an `EventSource`.

Shape of the module:

- `connect()` — idempotent. Returns immediately if a connection exists or is pending. Called
  once by the shell layout, not by each island.
- `subscribe(topic, handler)` — returns an unsubscribe function. Topics are the envelope
  topics from ARCHITECTURE §6 (`project.state_changed`, `project.priority.recalculated`,
  `project.risk.changed`, `blocker.raised`, ...), listed as constants in `stream/topics.ts`.
  Nothing string-literals a topic at the call site.
- `onStatus(handler)` — `connecting | open | disconnected`. `ConnectionBadge` is the only
  island that renders it; other islands read it to decide whether their data is stale.
- Handlers receive the parsed envelope: `{id, topic, occurred_at, actor, correlation_id,
  entity, payload, version}`.

The server frames each event with `id:` set to `event.id` and `event:` set to the topic, so
`es.addEventListener(topic, ...)` works and the browser tracks the last id. The store keeps
its own copy of the last seen id because a manual reconnect builds a new `EventSource`, and
a new object does not carry the previous `Last-Event-ID`.

## Reconnection

`EventSource` retries on its own only while the object lives. On `onerror` with
`readyState === CLOSED`, the store closes it and schedules its own retry:

- delay `min(1000 * 2 ** attempt, 30000)` with jitter, attempt reset to 0 on `open`;
- new connection is opened against `/api/stream?last_event_id=<id>` using the stored id, so
  the backend replays from that point instead of the tail;
- status goes `disconnected` immediately on failure, not after the retry fails, so the UI
  tells the truth while it is down;
- `retry(): void` exposed for the manual button, which cancels the pending timer and connects
  now with `attempt = 0`.

## Typed API client

Every HTTP call goes through `frontend/src/lib/api/client.ts`, from page frontmatter and from
islands alike. It owns the base URL, the actor header, JSON parsing, and the mapping of the
backend's typed domain errors (`TransitionNotAllowed` and friends) into a discriminated
`ApiError` the caller can branch on. A bare `fetch('/api/...')` inside a component is a bug —
the error contract and the types are lost.

Return type is `Result`-shaped, not a thrown exception, because the four view states are
driven by it:

```ts
const res = await api.getProject(code);
if (!res.ok) return renderError(res.error);
```

## Transition buttons

Legal transitions come from the backend with the project — the frontend never derives them
and never holds a list of state codes. For each entry in `project.transitions`:

- label from `transition.label`, never from the state code;
- disabled when a field in `transition.requires_fields` is empty on the project (e.g.
  `next_step`), with the reason shown next to the button;
- `requires_reason` opens a required-text dialog; submit is blocked while it is empty;
- POST through the client to `/api/projects/{code}/transition` with `{to_state, reason}`;
- on `ApiError` of kind `transition_not_allowed`, re-fetch the project rather than guessing —
  the local view was stale;
- do not optimistically paint the new state. The authoritative update arrives as
  `project.state_changed` on the stream; the button only shows a pending state until then.

## The four view states

Every view and every island renders all four. A component that only handles the happy path
does not ship.

1. **Loading** — skeleton rows matching the final layout height, no spinner-only screens.
2. **Empty** — explicit sentence about what is empty plus the action that fills it
   ("No open blockers"), not a blank region.
3. **Error** — what failed, and a retry that re-runs the same client call.
4. **SSE-disconnected** — content stays visible but is marked stale, with the timestamp of
   the last event and a visible "Reconnect" wired to `retry()`. Never blank the screen and
   never silently show stale numbers as if they were live.

`frontend/src/lib/view-state.ts` holds the union and the helper that maps an api `Result` plus the
store status onto it, so the four cases are exhaustive-checked instead of reimplemented.

## Common mistakes

- One `EventSource` per island. Browsers cap connections per origin and the backend fans out
  the same stream N times. Subscribe to the store.
- Calling `connect()` from each island instead of once from the layout, producing several
  connections during hydration.
- `fetch()` inside a component, bypassing `lib/api/client.ts`: no types, no actor header, no
  error mapping.
- Hardcoding state codes, state labels, priority names or colors in the frontend. All of it
  is taxonomy data from the API (`code`, `label`, `color`) — see hard rule 1.
- Building transition buttons from a static list, or enabling one whose `requires_fields`
  are unmet, which turns a validation into a 4xx round trip.
- Rendering the priority score without its `breakdown`. The number alone is not defensible;
  §4.1 exists so the UI can show the per-signal reason.
- Showing a `PriorityOverride` as if it were a computed score instead of labelling it manual.
- Omitting the SSE-disconnected state, or collapsing it into the error state — they need
  different affordances.
- Hydrating static components (timeline, score breakdown, task table) with `client:load`
  because it was easier than patching them from the parent island.
- Reconnecting without `last_event_id`, which silently drops every event that occurred while
  the tab was offline.
- Assuming a topic arrives once. Delivery is at-least-once; handlers must be safe to run
  twice with the same `event.id`.
