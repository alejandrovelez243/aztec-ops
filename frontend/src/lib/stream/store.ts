/**
 * The single SSE connection for the whole tab — the only module allowed to construct an
 * `EventSource` (FRONTEND.md §5 SRP). Islands subscribe to topics through this store; an
 * island that opens its own connection gets a second `Last-Event-ID` cursor and duplicate
 * patches.
 *
 * Invariants:
 *
 * - **Refcount.** The first `subscribe()` opens the connection; the last unsubscribe closes
 *   it. `connect()` is idempotent and is also called once by the shell layout so the stream
 *   is warm before any island hydrates and the connection badge has a status to render.
 * - **Dedupe.** Delivery is at-least-once (docs/EVENTS.md §6), so envelopes are deduplicated
 *   on `id` before dispatch. The seen-id buffer is bounded with FIFO eviction; an id that
 *   was evicted and is then redelivered is dispatched again, which is safe because handlers
 *   compare `occurred_at` against the rendered `updated_at` before patching.
 * - **Cursor.** `lastEventId` is tracked from every frame because a freshly constructed
 *   `EventSource` does not send the `Last-Event-ID` header; a store-driven reconnect passes
 *   it as `?last_event_id=` instead (docs/API.md §3.5). The transport cannot replay — Redis
 *   pub/sub is live-only — and answers that parameter with one `stream.reset` frame, which
 *   the store routes to `onReset` handlers so views refetch rather than trust local state.
 *
 * What leaks if cleanup is skipped: an island that discards the function returned by
 * `subscribe()` keeps its handler alive across an Astro view transition — the detached
 * island keeps patching rows that are no longer in the DOM — and the refcount never reaches
 * zero, so the connection stays open for a page that no longer displays anything from it.
 */
import { ensureFreshAccess } from "../api/client";
import { nextDelay } from "./backoff";
import { TOPICS, type Topic } from "./topics";

/**
 * The fixed event envelope (docs/EVENTS.md §1), parsed and narrowed from a frame's `data:`
 * line. `payload` stays `Record<string, unknown>`: the store does not interpret payloads —
 * each topic's schema is versioned independently and is the subscriber's business, not the
 * transport's.
 */
export interface Envelope {
  /** Event identity and the dedupe key; also the frame's `id:` field. */
  id: string;
  topic: Topic;
  /** When the change committed (ISO-8601 UTC), not when it was delivered. */
  occurred_at: string;
  actor: string;
  correlation_id: string;
  entity: { type: string; id: string };
  payload: Record<string, unknown>;
  version: number;
}

/**
 * Connection status surfaced to the UI. `disconnected` means no live data is arriving and
 * on-screen data is stale; it is set immediately when the connection drops, not after the
 * reconnection budget is exhausted, because an operator reading a stale board must know it
 * is stale.
 */
export type Status = "connecting" | "open" | "disconnected";

/** Subscriber callback for one topic. Receives the narrowed envelope, never raw JSON. */
export type EnvelopeHandler = (envelope: Envelope) => void;

/** Status callback, invoked on every transition and once immediately on registration. */
export type StatusHandler = (status: Status) => void;

const STREAM_BASE: string =
  import.meta.env.PUBLIC_API_URL ?? "http://localhost:8000";
const STREAM_PATH = "/api/stream";

/** Topic that never reaches topic subscribers; it routes to the reset handlers instead. */
const RESET_TOPIC: Topic = "stream.reset";

/** Upper bound on the dedupe buffer; beyond it the oldest ids are forgotten (FIFO). */
const SEEN_ID_LIMIT = 1000;

const TOPIC_SET: ReadonlySet<string> = new Set<string>(TOPICS);
const DOMAIN_TOPICS: readonly Topic[] = TOPICS.filter(
  (topic) => topic !== RESET_TOPIC,
);

let source: EventSource | null = null;
let status: Status = "disconnected";
let attempt = 0;
let reconnectTimer: ReturnType<typeof setTimeout> | null = null;
let lastEventId: string | null = null;
let subscriptionCount = 0;

const subscribers = new Map<Topic, Set<EnvelopeHandler>>();
const statusHandlers = new Set<StatusHandler>();
const resetHandlers = new Set<() => void>();
const seenIds = new Set<string>();
const seenOrder: string[] = [];

/**
 * Opens the shared connection, or does nothing if one is open, connecting, or awaiting its
 * scheduled retry. Called once by the shell layout; the first `subscribe()` also calls it,
 * so an island never needs to.
 */
export function connect(): void {
  if (source !== null || reconnectTimer !== null) return;
  openConnection();
}

/**
 * Subscribes a handler to one topic on the shared connection.
 *
 * The first subscriber opens the connection; the last unsubscribe closes it (refcount).
 * Envelopes are deduplicated on `id`, so the handler runs once per event even though
 * delivery is at-least-once. Subscribing to `stream.reset` throws — that topic carries no
 * envelope and must be consumed through `onReset`; a subscription to it would silently
 * never fire.
 *
 * The returned function is idempotent. Failing to call it on teardown leaks the handler
 * across view transitions and holds the connection open (see module docstring).
 *
 * @param topic - One of {@link TOPICS}, except `stream.reset`.
 * @param handler - Invoked with the narrowed envelope for each new event on the topic.
 * @returns The unsubscribe function; call it on island teardown.
 */
export function subscribe(topic: Topic, handler: EnvelopeHandler): () => void {
  if (topic === RESET_TOPIC) {
    throw new Error(
      "stream.reset carries no envelope; use onReset() instead of subscribe()",
    );
  }
  let handlers = subscribers.get(topic);
  if (handlers === undefined) {
    handlers = new Set();
    subscribers.set(topic, handlers);
  }
  handlers.add(handler);
  subscriptionCount += 1;
  if (subscriptionCount === 1) connect();

  let active = true;
  return () => {
    if (!active) return;
    active = false;
    const current = subscribers.get(topic);
    if (current !== undefined) {
      current.delete(handler);
      if (current.size === 0) subscribers.delete(topic);
    }
    subscriptionCount -= 1;
    if (subscriptionCount === 0) closeConnection();
  };
}

/**
 * Registers a status observer. The handler fires once immediately with the current status —
 * an island hydrating mid-outage must render stale chrome without waiting for the next
 * transition — and then on every change.
 *
 * @param handler - Invoked with the new status.
 * @returns A function that removes the observer; call it on island teardown.
 */
export function onStatus(handler: StatusHandler): () => void {
  statusHandlers.add(handler);
  safeInvoke(() => handler(status));
  return () => {
    statusHandlers.delete(handler);
  };
}

/**
 * Registers a handler for `stream.reset`, the frame the server sends when asked to resume
 * from an id it cannot replay (Redis pub/sub has no history). It means local state can no
 * longer be trusted: handlers are expected to refetch the resources they render, not to
 * patch from the stream.
 *
 * @param handler - Invoked with no arguments; the reset carries no entity to act on.
 * @returns A function that removes the handler; call it on island teardown.
 */
export function onReset(handler: () => void): () => void {
  resetHandlers.add(handler);
  return () => {
    resetHandlers.delete(handler);
  };
}

/**
 * Reconnects now: cancels the pending backoff timer, resets the attempt counter, drops any
 * half-open connection and opens a fresh one. This is the wiring behind the "Reconnect"
 * control of the disconnected view state.
 */
export function retry(): void {
  cancelReconnect();
  attempt = 0;
  if (source !== null) {
    source.close();
    source = null;
  }
  openConnection();
}

/**
 * The id of the last frame seen, or `null` before the first one. This is the value a manual
 * reconnect replays through `?last_event_id=`, and what a server-rendered page hands to its
 * islands so they can tell how far behind the stream they started.
 */
export function getLastEventId(): string | null {
  return lastEventId;
}

function openConnection(): void {
  setStatus("connecting");
  // An EventSource cannot carry an Authorization header; it authenticates with
  // the HttpOnly access cookie (config.auth), which the refresh response
  // re-sets. Renew first, best-effort, so the stream does not open with a
  // cookie that dies mid-connection — then connect regardless: a failed
  // refresh surfaces as the stream's own 401/close and the normal retry path.
  void ensureFreshAccess()
    .catch(() => null)
    .finally(() => {
      openEventSource();
    });
}

function openEventSource(): void {
  if (source !== null || reconnectTimer !== null) return;
  const next = new EventSource(streamUrl(), { withCredentials: true });
  source = next;
  next.onopen = () => {
    attempt = 0;
    setStatus("open");
  };
  next.onerror = () => {
    handleError(next);
  };
  next.onmessage = (event) => {
    handleDataFrame(event);
  };
  for (const topic of DOMAIN_TOPICS) {
    next.addEventListener(topic, (event) => {
      handleDataFrame(event);
    });
  }
  next.addEventListener(RESET_TOPIC, (event) => {
    handleResetFrame(event);
  });
}

function handleError(failed: EventSource): void {
  if (failed !== source) return;
  if (failed.readyState === EventSource.CLOSED) {
    // The browser has given up (e.g. a terminal HTTP status); the store takes over the
    // retry loop so the delay comes from our backoff, not the browser's fixed one.
    failed.close();
    source = null;
    setStatus("disconnected");
    scheduleReconnect();
    return;
  }
  // CONNECTING: the browser is retrying on its own, carrying the Last-Event-ID header it
  // tracked. Surface it as connecting — data is not flowing, but a recovery is in flight.
  setStatus("connecting");
}

function scheduleReconnect(): void {
  if (reconnectTimer !== null) return;
  const delay = nextDelay(attempt);
  attempt += 1;
  reconnectTimer = setTimeout(() => {
    reconnectTimer = null;
    openConnection();
  }, delay);
}

function cancelReconnect(): void {
  if (reconnectTimer !== null) {
    clearTimeout(reconnectTimer);
    reconnectTimer = null;
  }
}

function closeConnection(): void {
  cancelReconnect();
  attempt = 0;
  if (source !== null) {
    source.close();
    source = null;
  }
  setStatus("disconnected");
}

function streamUrl(): string {
  const url = new URL(STREAM_PATH, STREAM_BASE);
  // A constructed EventSource cannot send the Last-Event-ID header, so the cursor travels
  // as a query parameter with the same meaning (docs/API.md §3.5).
  if (lastEventId !== null) url.searchParams.set("last_event_id", lastEventId);
  return url.toString();
}

function handleDataFrame(event: Event): void {
  const message = asMessageEvent(event);
  if (message === null) return;
  trackLastEventId(message.lastEventId);
  const envelope = parseEnvelope(message.data);
  if (envelope === null) return;
  dispatch(envelope);
}

function handleResetFrame(event: Event): void {
  const message = asMessageEvent(event);
  if (message === null) return;
  if (!isResetPayload(message.data)) return;
  for (const handler of resetHandlers) safeInvoke(handler);
}

function dispatch(envelope: Envelope): void {
  if (seenIds.has(envelope.id)) return;
  remember(envelope.id);
  const handlers = subscribers.get(envelope.topic);
  if (handlers === undefined) return;
  for (const handler of handlers) {
    safeInvoke(() => handler(envelope));
  }
}

function remember(id: string): void {
  seenIds.add(id);
  seenOrder.push(id);
  if (seenOrder.length > SEEN_ID_LIMIT) {
    const oldest = seenOrder.shift();
    if (oldest !== undefined) seenIds.delete(oldest);
  }
}

function trackLastEventId(id: string): void {
  // The buffer starts as the empty string; only a frame that actually carried `id:` moves it.
  if (id !== "") lastEventId = id;
}

function setStatus(next: Status): void {
  if (status === next) return;
  status = next;
  for (const handler of statusHandlers) {
    safeInvoke(() => handler(next));
  }
}

/**
 * Parses and narrows the `unknown` JSON of a `data:` line into an envelope — the one place
 * `unknown` is allowed (FRONTEND.md §1). Anything that is not a well-formed envelope is
 * dropped: one malformed frame must not take down the stream for every island.
 */
function parseEnvelope(raw: unknown): Envelope | null {
  if (typeof raw !== "string") return null;
  let data: unknown;
  try {
    data = JSON.parse(raw);
  } catch {
    return null;
  }
  if (!isRecord(data)) return null;

  const id = data["id"];
  const topic = data["topic"];
  const occurredAt = data["occurred_at"];
  const actor = data["actor"];
  const correlationId = data["correlation_id"];
  const entityRaw = data["entity"];
  const payload = data["payload"];
  const version = data["version"];

  if (typeof id !== "string" || id === "") return null;
  if (!isTopic(topic)) return null;
  if (typeof occurredAt !== "string") return null;
  if (typeof actor !== "string") return null;
  if (typeof correlationId !== "string") return null;
  if (!isRecord(entityRaw)) return null;
  const entityType = entityRaw["type"];
  const entityId = entityRaw["id"];
  if (typeof entityType !== "string" || typeof entityId !== "string")
    return null;
  if (!isRecord(payload)) return null;
  if (typeof version !== "number") return null;

  return {
    id,
    topic,
    occurred_at: occurredAt,
    actor,
    correlation_id: correlationId,
    entity: { type: entityType, id: entityId },
    payload,
    version,
  };
}

/**
 * Narrows a `stream.reset` frame's `data:` line. The payload (`{"reason": ...}`) is not an
 * envelope and is not handed to anyone — its shape is checked only so a stray frame with
 * the same event name does not trigger a spurious refetch storm.
 */
function isResetPayload(raw: unknown): boolean {
  if (typeof raw !== "string") return false;
  let data: unknown;
  try {
    data = JSON.parse(raw);
  } catch {
    return false;
  }
  return isRecord(data);
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}

function isTopic(value: unknown): value is Topic {
  return typeof value === "string" && TOPIC_SET.has(value);
}

function asMessageEvent(event: Event): MessageEvent | null {
  return event instanceof MessageEvent ? event : null;
}

/**
 * Runs one subscriber, isolating its failure. A throwing handler is a bug in that island;
 * letting it propagate out of the dispatch loop would silently starve every subscriber
 * after it in the set.
 */
function safeInvoke(invoke: () => void): void {
  try {
    invoke();
  } catch (error) {
    console.error("aztec stream: a subscriber threw and was isolated", error);
  }
}
