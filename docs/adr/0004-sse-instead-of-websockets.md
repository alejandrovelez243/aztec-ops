# 0004 — SSE instead of WebSockets for live updates

## Status

Accepted — 2026-07-28.

## Context

The operations lead leaves the command center open while other people work. When someone raises
a blocker, moves a project's state or a recalculation changes the ranking, the screen has to
reflect it without a manual refresh — a triage view that silently shows yesterday's order is
worse than one that admits it is stale.

The traffic is entirely one-way. Every user action in this product is an HTTP request that must
be validated against a `WorkflowTransition`, may require a reason, and must write an
`ActivityRecord` and an `OutboxEvent` in one transaction. There is no interaction that benefits
from being sent over a persistent socket, and routing a transition through a socket frame would
mean re-implementing request validation, error mapping and idempotency outside the API layer.

The consumer side already exists: `sse-fanout` (ADR 0003) publishes to the Redis channel
`aztec.sse`. What remains is choosing the wire between the server and the browser. The relevant
pressures are that the demo runs behind Docker Compose and may run behind a proxy later, that
reconnection has to be reliable without hand-written code, and that the client is an Astro island
(ADR 0008) with a small budget for connection management.

## Decision

`GET /api/stream` is an ASGI endpoint returning `text/event-stream`. It subscribes to the Redis
`aztec.sse` channel and writes each event envelope to the response as an SSE message. The
browser consumes it with the native `EventSource`.

- One `EventSource` for the whole page, owned by the shared store in `frontend/src/lib/stream/`.
  Components subscribe to the store. A component that constructs its own `EventSource` is a bug.
- The client reconnects with exponential backoff, a cap and jitter, and shows connection status
  as a real rendered state with a retry control.
- After a reconnect the client refetches the affected resource rather than assuming continuity,
  because delivery is at-least-once and events may have been missed while offline.
- All writes stay on the JSON API. The stream is read-only.

## Consequences

Good:

- `EventSource` handles reconnection itself, so the baseline behaviour is correct before we
  write a line of it; our backoff logic is an improvement on a working default, not a
  prerequisite.
- It is plain HTTP. It survives proxies and corporate middleboxes that mishandle the WebSocket
  upgrade, needs no protocol negotiation in the compose setup, and can be debugged with `curl`
  against the endpoint — which is also how the RUNBOOK diagnoses "SSE does not update".
- One connection per page instead of one per component keeps the server holding a small, bounded
  number of long-lived responses.
- The read-only wire makes the security surface trivial: nothing arriving from the browser can
  reach the domain through it.

Cost we accepted:

- The server holds an open response per viewer. Under ASGI this is cheap, but it is a real
  resource, and a proxy with an idle-connection timeout will cut streams that carry no traffic —
  which is why the endpoint emits periodic keepalives and the client treats a silent stream as a
  disconnect.
- Browsers cap concurrent connections per origin over HTTP/1.1. Six tabs of the command center
  can exhaust it. Acceptable for a five-person operation, and it is why the single shared
  connection rule exists rather than being a style preference.
- No client-to-server channel means anything that later wants one — presence, "someone else is
  editing this project", collaborative cursors — needs a different transport, not an extension of
  this one. We are betting those never appear, and if they do this ADR gets superseded.
- Reconnect-and-refetch means the client sometimes does redundant work after a network blip. We
  prefer that to trusting `Last-Event-ID` to reconstruct a gap the server does not durably
  buffer.

## Alternatives considered

- **WebSockets.** Rejected. Bidirectionality we do not use, in exchange for manual reconnection,
  heartbeat and backoff logic, a protocol upgrade that proxies handle inconsistently, and — with
  Django — either Channels and its own layer or a second server process. Every one of those is a
  cost paid for a capability the product has no use for.
- **Polling the read API on an interval.** Rejected. `ProjectSnapshot` makes a poll cheap, so
  this is genuinely tempting. It loses on latency during triage (a blocker raised now should not
  wait for the next tick) and on honesty: polling cannot distinguish "nothing changed" from
  "the backend stopped producing events", so the UI cannot render a truthful disconnected state.
- **Long polling.** Rejected. It is SSE with worse ergonomics and hand-rolled reconnection.
