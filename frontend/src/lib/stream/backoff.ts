/**
 * Reconnect backoff for the shared SSE store.
 *
 * The sequence is exponential (`1000 * 2 ** attempt` ms) with ±25% jitter, then clamped:
 * floored at 3000 ms — the `retry: 3000` hint the server sends when the stream opens
 * (`backend/config/sse.py`), so the client never reconnects faster than the server asked —
 * and capped at 30000 ms so a long outage does not push the next attempt beyond what an
 * operator staring at a stale board will tolerate. The jitter keeps every open tab from
 * reconnecting in lockstep after a backend restart, which would turn one restart into a
 * self-inflicted connection storm.
 */

/** First step of the exponential sequence, before the floor is applied. */
const BASE_MS = 1000;

/** Server-chosen floor: the `retry: 3000` frame sent on connect. */
const FLOOR_MS = 3000;

/** Upper bound on any single wait. */
const CAP_MS = 30000;

/** Jitter applied symmetrically to the computed delay, as a fraction of it. */
const JITTER_RATIO = 0.25;

/**
 * Milliseconds to wait before reconnect attempt `attempt` (0-based).
 *
 * Not deterministic — the jitter is drawn on every call, which is the point. The store
 * resets `attempt` to 0 on a successful `open`, so the floor is what a recovered connection
 * costs and the cap is what a dead one costs.
 *
 * @param attempt - 0-based count of consecutive failed connection attempts.
 * @returns The delay in milliseconds, in `[FLOOR_MS, CAP_MS]`.
 */
export function nextDelay(attempt: number): number {
  const exponential = BASE_MS * 2 ** Math.max(0, attempt);
  const jitter = exponential * JITTER_RATIO * (Math.random() * 2 - 1);
  return Math.min(CAP_MS, Math.max(FLOOR_MS, exponential + jitter));
}
