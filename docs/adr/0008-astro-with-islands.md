# 0008 — Astro 7 with islands instead of an SPA

## Status

Accepted — 2026-07-28.

## Context

The frontend is two views: `/` (command center) and `/projects/{code}` (detail). That is the
whole surface. There is no routing story worth a router, no client-side cache to invalidate, no
offline mode, no forms beyond a transition dialog that collects a reason.

Most of what those two views render is not live. The task list, the field values, the timeline,
the owner-load table and the score breakdown are all a snapshot of server state at request time.
`ProjectSnapshot` (`docs/ARCHITECTURE.md` §8) exists precisely so that snapshot is one query,
which means the server can render the full page immediately with no waterfall of API calls.

Only a few regions actually change without a navigation: the prioritized queue when a score is
recalculated, the project header when a state changes, the blockers panel, and the connection
badge. Those are the regions that consume the SSE stream from ADR 0004.

The usage pattern reinforces it. The operations lead opens this first thing in the morning, for
short repeated sessions, to triage — not to browse. A blank page followed by a spinner followed
by data is the wrong first impression for a tool whose job is to be immediately readable. And
the reviewer's five-minute recording shows the first paint more prominently than anything else.

## Decision

Astro 7 (Node >= 22.12) with server rendering by default. A component gets client JavaScript
only if it re-renders from an SSE event or takes user input.

- `frontend/src/components/` holds plain `.astro` components, rendered once on the server with no
  client script: `ScoreBreakdown`, `RiskFlags`, `OwnerLoad`, `Timeline`.
- `frontend/src/islands/` holds the live regions, each with one colocated script: `PriorityQueue`,
  `ProjectHeader`, `BlockerPanel`, `ConnectionBadge`. `client:load` above the fold,
  `client:visible` for live panels below it.
- One `EventSource` per page, owned by the store in `frontend/src/lib/stream/`. Islands subscribe to
  the store; an island that constructs its own connection is a bug.
- Every request goes through the typed client in `frontend/src/lib/api/client.ts`. No `fetch` inside
  a component or page. `frontend/src/lib/api/types.ts` is generated from the Ninja OpenAPI document
  with `npx openapi-typescript`, never hand-written.
- Four rendered states on every view: loading, empty, error, and SSE-disconnected with a visible
  retry.
- Transition buttons are rendered from the legal transitions the API returns, with their
  `label`, `to_state`, `requires_reason` and `requires_fields`. Never a hardcoded list.

## Consequences

Good:

- First paint is server-rendered HTML containing the actual ranking. The page is readable before
  any JavaScript executes, which is the correct behaviour for a triage screen.
- The JavaScript that ships is the SSE store plus four islands. There is no framework runtime
  carrying two static views.
- The static/live boundary is explicit in the directory layout, so "does this need to be live"
  is a decision made once per component rather than an emergent property of a component tree.
- Server rendering reads `ProjectSnapshot` directly, so the main view is one query with no
  client-side fetch waterfall.

Cost we accepted:

- State does not survive navigation. Moving from `/` to a project detail is a real page load: the
  `EventSource` closes and reopens, and any in-island state is gone. For two views entered by
  keyboard or click a few times a session, that is acceptable; for a tenth view it would not be.
- There are effectively two rendering contexts for the same data. A field displayed in both a
  static component and an island is formatted in two places, and they can drift. Shared
  formatting helpers are the only thing preventing it.
- Islands do not share state through a framework. Cross-island coordination goes through the
  store, and a piece of state that two islands both need is more awkward than it would be in a
  component tree.
- The generated `types.ts` is a manual regeneration step. Skip it after an API change and the
  types silently describe the old schema — the compiler will happily agree with a stale file.
- If a future view needs genuine client-side routing, optimistic updates or a shared client
  cache, this decision is the wrong foundation and gets superseded rather than extended.

## Alternatives considered

- **A full SPA (React or Vue with a router).** Rejected. A router, a data-fetching layer and a
  framework runtime for two views, paid for on every load, in exchange for preserving an
  `EventSource` across a navigation that happens a handful of times per session. It also makes
  the first paint a spinner, which is the opposite of what a morning triage screen should do.
- **Next.js or Nuxt.** Rejected. Both would work, but they bring a full application framework
  and its rendering model for a surface that needs server-rendered HTML plus four live regions.
  Astro's default — ship no JavaScript unless asked — matches the actual requirement, where
  theirs is opt-out.
- **Django templates with sprinkles of JavaScript.** Rejected. It would delete the frontend
  build entirely, and it stays genuinely tempting. It loses on the typed client generated from
  the OpenAPI document, which is the thing that keeps the UI honest about what the API returns,
  and on having a real component model for the score breakdown and risk flags that appear in
  both views.
- **htmx over the same Django templates.** Rejected. It handles the live-region problem well,
  but SSE-driven partial updates plus a connection-status UI plus the shared-store rule end up
  reimplemented in attributes, and the typed-client argument above still applies.
