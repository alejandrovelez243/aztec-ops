/**
 * Persisted JWT session state — storage only, no HTTP.
 *
 * This module is deliberately import-free so both `api/client.ts` (which
 * attaches the token and refreshes it) and `auth/session.ts` (which signs in
 * and out) can depend on it without a cycle.
 *
 * Invariants:
 *
 * - The whole session persists under one versioned key, so a shape change
 *   invalidates old sessions instead of half-reading them.
 * - `expiresAt` is an absolute epoch-ms instant computed by the writer from
 *   the API's relative `expires_in` — the value a skewed client clock can
 *   still compare against monotonically enough for a 30s skew margin.
 * - Reading never throws: a malformed or truncated stored value is treated as
 *   signed-out and cleared, because a session that cannot be parsed cannot be
 *   trusted either.
 */

/** Versioned localStorage key holding the whole {@link StoredSession}. */
export const SESSION_STORAGE_KEY = "aztec.session.v1";

/**
 * Cookie mirroring the access token onto the *frontend's* origin.
 *
 * The API sets its own `aztec_access` cookie, but that one is scoped to the
 * API's origin and path, so the Astro server never receives it. This mirror is
 * what makes server-rendered first paint possible: the middleware reads it and
 * hands it to the render (`./server-token.ts`).
 *
 * It is readable by JavaScript, necessarily — the browser is what writes it —
 * which is the same exposure the session already has in `localStorage`, and no
 * more. It is not the API's credential of record: writes still travel with the
 * `Authorization` header, which no cross-origin page can forge.
 */
export const SESSION_COOKIE_NAME = "aztec_front_token";

/** Safety margin subtracted from the expiry when judging freshness. */
const EXPIRY_SKEW_MS = 30_000;

/** Who the session belongs to, as the API reported it at sign-in. */
export interface SessionActor {
  readonly alias: string;
  readonly label: string;
  readonly role: string | null;
}

/** Everything the client persists about one signed-in session. */
export interface StoredSession {
  readonly access: string;
  readonly refresh: string;
  /** Epoch ms after which `access` must be refreshed before use. */
  readonly expiresAt: number;
  readonly actor: SessionActor;
  readonly isOpsLead: boolean;
}

/**
 * Reads the stored session, or `null` when signed out, storage is
 * unavailable (SSR), or the stored value does not parse as a session.
 */
export function loadSession(): StoredSession | null {
  if (typeof window === "undefined") return null;
  const raw = window.localStorage.getItem(SESSION_STORAGE_KEY);
  if (raw === null) return null;
  const parsed = parseSession(raw);
  if (parsed === null) {
    window.localStorage.removeItem(SESSION_STORAGE_KEY);
    return null;
  }
  return parsed;
}

/**
 * Persists the session, and mirrors its access token into
 * {@link SESSION_COOKIE_NAME} so the next server-rendered navigation can read
 * it. A no-op outside the browser.
 */
export function saveSession(session: StoredSession): void {
  if (typeof window === "undefined") return;
  window.localStorage.setItem(SESSION_STORAGE_KEY, JSON.stringify(session));
  writeTokenCookie(session);
}

/** Forgets the session and expires the mirror cookie. A no-op outside the browser. */
export function clearSession(): void {
  if (typeof window === "undefined") return;
  window.localStorage.removeItem(SESSION_STORAGE_KEY);
  document.cookie = `${SESSION_COOKIE_NAME}=; path=/; max-age=0; SameSite=Lax`;
}

/**
 * Writes the mirror cookie with the token's own remaining lifetime, so a stale
 * token expires with the cookie instead of outliving it and producing a first
 * paint the server believes is authenticated.
 */
function writeTokenCookie(session: StoredSession): void {
  const maxAge = Math.max(
    0,
    Math.floor((session.expiresAt - Date.now()) / 1000),
  );
  document.cookie = `${SESSION_COOKIE_NAME}=${encodeURIComponent(session.access)}; path=/; max-age=${maxAge}; SameSite=Lax`;
}

/**
 * Whether the stored access token can still be presented as-is. False within
 * {@link EXPIRY_SKEW_MS} of expiry so a request never departs with a token
 * that dies in flight.
 */
export function isAccessFresh(session: StoredSession): boolean {
  return Date.now() < session.expiresAt - EXPIRY_SKEW_MS;
}

/** Narrow a raw stored string into a session, or `null`. Never throws. */
function parseSession(raw: string): StoredSession | null {
  let data: unknown;
  try {
    data = JSON.parse(raw);
  } catch {
    return null;
  }
  if (typeof data !== "object" || data === null) return null;
  const record = data as Record<string, unknown>;
  const actor = record["actor"];
  if (typeof actor !== "object" || actor === null) return null;
  const actorRecord = actor as Record<string, unknown>;
  const alias = actorRecord["alias"];
  const label = actorRecord["label"];
  const role = actorRecord["role"];
  if (
    typeof record["access"] !== "string" ||
    typeof record["refresh"] !== "string" ||
    typeof record["expiresAt"] !== "number" ||
    typeof record["isOpsLead"] !== "boolean" ||
    typeof alias !== "string" ||
    typeof label !== "string" ||
    (typeof role !== "string" && role !== null)
  ) {
    return null;
  }
  return {
    access: record["access"],
    refresh: record["refresh"],
    expiresAt: record["expiresAt"],
    actor: { alias, label, role },
    isOpsLead: record["isOpsLead"],
  };
}
