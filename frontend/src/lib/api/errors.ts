/**
 * Typed errors of the Aztec Ops API client.
 *
 * The backend answers every non-2xx with one envelope — `{code, message, details}`
 * (`docs/API.md` §1.5) — and callers branch on `code`, never on `message`, which is
 * generated English prose for logs (`docs/API.md` §4.2). This module owns the mapping
 * from that envelope onto the `ApiError` union; the envelope itself is not part of the
 * generated OpenAPI components, so its shape is declared here against the documented
 * contract rather than derived from `types.ts`.
 */

/**
 * Machine codes a caller may switch on.
 *
 * The first four are the backend's documented domain codes (`docs/API.md` §1.5);
 * `network_error` and `unknown_error` are synthesized by the client for failures that
 * never produced an envelope — a refused connection, or a body that is not the
 * documented shape (a bare 500 means a bug and its body is not guaranteed).
 */
export type ApiErrorCode =
  | "transition_not_allowed"
  | "conflicting_state"
  | "validation_error"
  | "not_found"
  | "network_error"
  | "unknown_error";

/**
 * A rejected transition (HTTP 409 `transition_not_allowed`).
 *
 * `allowed` carries the legal `to_state` codes from `details.allowed`: a transition bar
 * whose buttons went stale resyncs from this list instead of refetching the project
 * (`docs/API.md` §2.5).
 */
export interface TransitionNotAllowedError {
  kind: "transition_not_allowed";
  code: "transition_not_allowed";
  message: string;
  allowed: string[];
  details: Record<string, unknown>;
}

/**
 * A rejected payload or actor (HTTP 422 `validation_error`).
 *
 * `fields` is `details.fields` narrowed to `Record<string, string[]>` — field name to
 * the messages that failed, including the `X-Actor` entry the middleware emits when the
 * actor header is missing or names no user. It is `{}` when the server sent none.
 */
export interface ValidationError {
  kind: "validation";
  code: "validation_error";
  message: string;
  fields: Record<string, string[]>;
  details: Record<string, unknown>;
}

/** An unknown project, task or blocker identifier (HTTP 404 `not_found`). */
export interface NotFoundError {
  kind: "not_found";
  code: "not_found";
  message: string;
  details: Record<string, unknown>;
}

/**
 * The request never reached a HTTP status: DNS, connection refused, abort.
 *
 * There is no envelope to parse, so this arm carries no `details`; retry policy is the
 * caller's decision, not something the body could have described.
 */
export interface NetworkError {
  kind: "network";
  code: "network_error";
  message: string;
}

/**
 * Everything else: a documented code with no dedicated arm (`conflicting_state`), an
 * undocumented code, or a non-2xx whose body is not the envelope at all.
 *
 * `code` preserves `conflicting_state` when that is what arrived — resolving an
 * already-resolved blocker is answered with it (`docs/API.md` §2.11) and the UI needs
 * to tell it apart from a genuine bug. `backendCode` keeps the raw envelope code when
 * it is not one of the documented values, so a code added server-side tomorrow is
 * observable in logs rather than silently swallowed.
 */
export interface UnknownError {
  kind: "unknown";
  code: ApiErrorCode;
  backendCode: string | null;
  message: string;
  details: Record<string, unknown>;
}

/**
 * Every failure a client function can return, discriminated on `kind`.
 *
 * Client functions never throw: a `try/catch` around a client call means somebody made
 * it throw, and is a bug (`docs/standards/PATTERNS_FRONTEND.md` §3).
 */
export type ApiError =
  | TransitionNotAllowedError
  | ValidationError
  | NotFoundError
  | NetworkError
  | UnknownError;

/** Narrows an `unknown` JSON value to a plain object; arrays and null are not records. */
function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/** Keeps only the string entries of an `unknown` list; anything else means the wire lied. */
function readStringList(value: unknown): string[] {
  if (!Array.isArray(value)) return [];
  return value.filter((entry): entry is string => typeof entry === "string");
}

/**
 * Narrows `details.fields` to `Record<string, string[]>`, dropping malformed entries.
 *
 * A field whose messages are not a list of strings is discarded rather than repaired:
 * the envelope is the contract, and inventing a message would put words in the server's
 * mouth.
 */
function readFieldErrors(value: unknown): Record<string, string[]> {
  if (!isRecord(value)) return {};
  const fields: Record<string, string[]> = {};
  for (const [name, messages] of Object.entries(value)) {
    if (
      Array.isArray(messages) &&
      messages.every((m) => typeof m === "string")
    ) {
      fields[name] = messages;
    }
  }
  return fields;
}

/**
 * Maps a non-2xx response onto the `ApiError` union.
 *
 * A body that is not the documented envelope — including the bare 500 that is outside
 * the contract — becomes `unknown_error` carrying the HTTP status, never a throw.
 */
export function errorFromResponse(status: number, body: unknown): ApiError {
  if (!isRecord(body)) {
    return {
      kind: "unknown",
      code: "unknown_error",
      backendCode: null,
      message: `HTTP ${status} returned no error envelope.`,
      details: {},
    };
  }
  const code = typeof body["code"] === "string" ? body["code"] : null;
  const message =
    typeof body["message"] === "string" ? body["message"] : `HTTP ${status}.`;
  const details = isRecord(body["details"]) ? body["details"] : {};
  if (code === null) {
    return {
      kind: "unknown",
      code: "unknown_error",
      backendCode: null,
      message,
      details,
    };
  }
  switch (code) {
    case "transition_not_allowed":
      return {
        kind: "transition_not_allowed",
        code,
        message,
        allowed: readStringList(details["allowed"]),
        details,
      };
    case "validation_error":
      return {
        kind: "validation",
        code,
        message,
        fields: readFieldErrors(details["fields"]),
        details,
      };
    case "not_found":
      return { kind: "not_found", code, message, details };
    case "conflicting_state":
      return { kind: "unknown", code, backendCode: code, message, details };
    default:
      return {
        kind: "unknown",
        code: "unknown_error",
        backendCode: code,
        message,
        details,
      };
  }
}

/**
 * Wraps a `fetch` rejection as a `network` error.
 *
 * The cause is `unknown` because `catch` binds are; the message is preserved verbatim
 * since it is the only diagnostic a refused connection leaves behind.
 */
export function errorFromNetwork(cause: unknown): ApiError {
  const message = cause instanceof Error ? cause.message : String(cause);
  return { kind: "network", code: "network_error", message };
}
