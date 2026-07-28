/**
 * The topics a browser may subscribe to on `GET /api/stream`.
 *
 * The eleven domain topics are exactly the `sse-fanout` allowlist (docs/EVENTS.md §5): each
 * one changes something a view renders. `stream.reset` is not a domain topic — it is emitted
 * by the stream endpoint itself (`backend/config/sse.py`) when a reconnect asks to resume
 * from an id the transport cannot replay — and the store routes it to `onReset` handlers
 * instead of topic subscribers. It is listed here so the topic vocabulary has one home.
 *
 * A topic the backend publishes that is missing from this list is received and silently
 * discarded (docs/EVENTS.md §5): adding a topic to the fanout without adding it here is a
 * no-op by design, and this list is the client half of that contract.
 */
export const TOPICS = [
  "project.created",
  "project.updated",
  "project.state_changed",
  "project.priority.recalculated",
  "project.risk.changed",
  "task.created",
  "task.updated",
  "task.state_changed",
  "blocker.raised",
  "blocker.resolved",
  "note.added",
  "stream.reset",
] as const;

/**
 * One subscribable stream topic. Call sites never string-literal a topic — a topic that is
 * not in {@link TOPICS} does not compile, which is what keeps a renamed backend topic from
 * becoming a silent no-subscription at runtime.
 */
export type Topic = (typeof TOPICS)[number];
