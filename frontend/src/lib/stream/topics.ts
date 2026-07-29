/**
 * The topics a browser may subscribe to on `GET /api/stream`.
 *
 * The ten live domain topics are exactly the `sse-fanout` allowlist (docs/EVENTS.md §5): each
 * one changes something a view renders. `stream.reset` is not a domain topic — it is emitted
 * by the stream endpoint itself (`backend/config/sse.py`) when a reconnect asks to resume
 * from an id the transport cannot replay — and the store routes it to `onReset` handlers
 * instead of topic subscribers. It is listed here so the topic vocabulary has one home.
 *
 * A topic the backend publishes that is missing from this list is received and silently
 * discarded (docs/EVENTS.md §5): adding a topic to the fanout without adding it here is a
 * no-op by design, and this list is the client half of that contract.
 *
 * `project.risk.changed` is **retired and is never emitted** — risk flags are computed on read
 * (docs/adr/0011-risk-flags-computed-on-read.md), so they have no moment of change to announce.
 * Every project payload carries its current `risk_flags` and `health`, and a view refreshes them
 * by re-reading the project named by any of the write-side topics below. The one change with no
 * frame behind it is the calendar: a project that goes overdue at midnight is correct on the next
 * load. The name is kept in this list only so the islands that still listen for it keep compiling;
 * their handlers are dead code and the migration is owed. Do not add new subscribers.
 */
export const TOPICS = [
  "project.created",
  "project.updated",
  "project.state_changed",
  "project.priority.recalculated",
  // Retired: never delivered. See the note above; kept so existing subscribers compile.
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
