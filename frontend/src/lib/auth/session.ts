/**
 * Session flows: sign in, sign out, guard, and who the operator is.
 *
 * Storage lives in `./tokens.ts`; HTTP lives in `../api/client.ts`. This
 * module composes the two, so it is the only place the login page and the
 * shell need to import.
 *
 * Failure mode worth knowing: `signOut` clears local state even when the
 * logout POST fails — a dead network must never trap someone in a session —
 * and the access cookie then simply expires on its own within minutes, which
 * the backend documents as acceptable.
 */

import { postLogout, postToken } from "../api/client";
import type { ApiError } from "../api/errors";
import {
  clearSession,
  loadSession,
  saveSession,
  SESSION_COOKIE_NAME,
  type SessionActor,
  type StoredSession,
} from "./tokens";

/** What the shell renders about the signed-in operator. */
export interface Operator extends SessionActor {
  readonly isOpsLead: boolean;
}

/** Where the login page lives; the guard's redirect target. */
export const LOGIN_PATH = "/login";

/**
 * Exchanges credentials for a session and persists it.
 *
 * @returns `ok: true` when signed in; otherwise the typed `ApiError`
 *   (`invalid_credentials` covers unknown user, wrong password and inactive
 *   account in one answer, by backend design).
 */
export async function signIn(
  username: string,
  password: string,
): Promise<{ ok: true } | { ok: false; error: ApiError }> {
  const result = await postToken({ username, password });
  if (!result.ok) return result;
  const pair = result.data;
  const session: StoredSession = {
    access: pair.access,
    refresh: pair.refresh,
    expiresAt: Date.now() + pair.expires_in * 1000,
    actor: {
      alias: pair.actor.alias,
      label: pair.actor.label,
      role: pair.actor.role ?? null,
    },
    isOpsLead: pair.is_ops_lead,
  };
  saveSession(session);
  return { ok: true };
}

/**
 * Ends the session: best-effort logout POST (clears the SSE cookie), then
 * local state, then the login screen. Never throws and never blocks on the
 * network to let go.
 */
export async function signOut(): Promise<void> {
  try {
    await postLogout();
  } finally {
    clearSession();
    window.location.assign(LOGIN_PATH);
  }
}

/** The signed-in operator, or `null`. */
export function getOperator(): Operator | null {
  const session = loadSession();
  if (session === null) return null;
  return { ...session.actor, isOpsLead: session.isOpsLead };
}

/**
 * Whether a session exists that the *server* would also honour.
 *
 * Both halves are checked, and the conjunction is the point: the stored
 * session is what the client fetches with, and the mirror cookie is what the
 * middleware reads. If they disagree, the two guards send the visitor in
 * opposite directions and the tab ping-pongs between `/` and `/login` — a
 * redirect loop that presents itself as a page that never loads. A session
 * without its cookie is therefore reported as absent, and the login screen
 * clears it (`lib/auth/guard-inline.ts`).
 */
export function hasSession(): boolean {
  if (loadSession() === null) return false;
  return document.cookie.includes(`${SESSION_COOKIE_NAME}=`);
}

/**
 * Client-side route guard for app pages, re-run after every view transition:
 * without a session the location is replaced with the login screen carrying a
 * `next` back-reference. The authoritative guard is the middleware; this one
 * catches an expiry that happens while the tab is open, with no navigation to
 * the server in between.
 */
export function guardAppPage(): boolean {
  if (hasSession()) return true;
  const next = window.location.pathname + window.location.search;
  window.location.replace(`${LOGIN_PATH}?next=${encodeURIComponent(next)}`);
  return false;
}

/** Initials for an avatar disk: first letters of the first two words. */
export function initials(label: string): string {
  const parts = label.trim().split(/\s+/).slice(0, 2);
  return parts.map((part) => part.charAt(0).toUpperCase()).join("");
}

/**
 * Deterministic avatar hue (0–359) from an alias, so one person keeps one
 * color everywhere without storing anything.
 */
export function avatarHue(alias: string): number {
  let hash = 0;
  for (let i = 0; i < alias.length; i += 1) {
    hash = (hash * 31 + alias.charCodeAt(i)) % 360;
  }
  return hash;
}
