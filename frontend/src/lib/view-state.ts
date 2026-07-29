import type { ApiError, ApiErrorCode } from "./api/errors";
import type { Result } from "./api/client";
import type { Status } from "./stream/store";

/**
 * The single state machine every view renders.
 *
 * One discriminated union instead of independent `isLoading` / `hasError` / `items.length`
 * booleans, because the boolean form admits states that cannot exist (loading AND error) and
 * silently drops the ones that must be handled (`empty`, `disconnected`). Every consumer
 * switches on `kind` exhaustively and ends in `assertNever`, so adding an arm here fails the
 * build at every switch that does not handle it.
 *
 * `disconnected` carries the last known `data` plus `lastEventAt` on purpose: the board keeps
 * rendering stale rows marked with the timestamp of the last event instead of blanking, and
 * offers a retry wired to the stream store's `retry()`.
 */
export type ViewState<T> =
  | { kind: "loading" }
  | { kind: "ready"; data: T; lastEventAt: string | null }
  | { kind: "empty"; message: string }
  | { kind: "error"; code: ApiErrorCode; message: string }
  | { kind: "disconnected"; data: T; lastEventAt: string | null };

/**
 * Exhaustiveness guard for `switch (state.kind)` with no `default:` arm.
 *
 * Call it with the switch's fallthrough value: while the switch covers every arm of
 * `ViewState`, the value is `never` and the call compiles; the moment a new arm is added to
 * the union, every switch missing it stops compiling instead of silently rendering nothing.
 *
 * Reaching this at runtime means the invariant was broken by a value that bypassed the type
 * system (an unparsed SSE payload, a hand-cast literal) — throwing surfaces it loudly rather
 * than rendering a blank view.
 */
export function assertNever(value: never): never {
  throw new Error(`Unhandled view state: ${JSON.stringify(value)}`);
}

/**
 * Options for {@link toViewState}.
 */
export interface ToViewStateOptions<T> {
  /**
   * Decides whether a successful payload is the `empty` state. Emptiness is domain-specific
   * (`items.length === 0` for the queue, "no open blockers" for the panel), so the caller
   * supplies the predicate and the union — not a boolean beside it — represents the outcome.
   */
  isEmpty: (data: T) => boolean;
  /** Copy for the `empty` arm: what is empty plus the action that fills it. */
  emptyMessage: string;
  /**
   * `occurred_at` of the most recent envelope the store has delivered, or `null` before the
   * first event. Carried into `ready` and `disconnected` so the view can timestamp its data.
   */
  lastEventAt: string | null;
}

/**
 * Maps an api `Result` plus the stream status onto the view-state union.
 *
 * Precedence, and why:
 *
 * - A failed fetch is `error` even while disconnected — there is no data to keep on screen,
 *   so the "stale but visible" contract of `disconnected` cannot be honoured. The error arm's
 *   retry re-runs the fetch, which is also the recovery from the dropped stream.
 * - `status === "disconnected"` with a successful result keeps the last data and its
 *   `lastEventAt`: the operator sees the board go stale instead of watching it blank. An
 *   empty payload stays `empty` even here — there is no stale content worth preserving.
 * - `"connecting"` with data is `ready`, not `loading`: `loading` belongs to the fetch that
 *   has not returned yet, and a fresh server response is authoritative regardless of whether
 *   the stream has finished opening.
 *
 * The `code` and `message` on the `error` arm come from the typed `ApiError` — the view maps
 * `code` to its own Spanish copy and never renders `message` as UI text (`docs/API.md` §1.5).
 */
export function toViewState<T>(
  result: Result<T>,
  status: Status,
  options: ToViewStateOptions<T>,
): ViewState<T> {
  if (!result.ok) {
    return toErrorState(result.error);
  }
  if (options.isEmpty(result.data)) {
    return { kind: "empty", message: options.emptyMessage };
  }
  if (status === "disconnected") {
    return {
      kind: "disconnected",
      data: result.data,
      lastEventAt: options.lastEventAt,
    };
  }
  return { kind: "ready", data: result.data, lastEventAt: options.lastEventAt };
}

/**
 * Narrows the `ApiError` union into the `error` arm, so `toViewState` stays a flat sequence
 * of guards and the `kind` discriminator is the single source of the error code.
 */
function toErrorState(error: ApiError): ViewState<never> {
  return { kind: "error", code: error.code, message: error.message };
}
