/**
 * The team, as every person picker reads it — `GET /api/v1/team/load`.
 *
 * Fetched in the browser rather than baked into the page, and read the first
 * time a picker opens, which is also the first moment it is needed.
 *
 * One read serves every picker on the page — the project header's, one per task
 * row, the activity feed's actor filter — through the single-flight promise
 * below. Twenty controls must not mean twenty requests for a list of five
 * people.
 *
 * Success and emptiness are cached for the life of the page; a failure is not,
 * so closing and reopening the menu is a retry rather than a second look at the
 * same error.
 *
 * The `error` arm carries the typed `ApiError`, never rendered prose: each
 * surface maps it to its own words (`failureCopy` on the projects surfaces,
 * `errorCopy` elsewhere), which is what lets this module live in `lib/` and be
 * shared instead of owning one view's vocabulary.
 */

import { getTeamLoad } from "../api/client";
import type { ApiError } from "../api/errors";
import type { TeamLoadEntry } from "../api/domain";

/**
 * The four arms every fetching surface owes its reader
 * (`docs/standards/FRONTEND.md` §1). `empty` is a real answer here — a roster
 * nobody has seeded — and it is not an error.
 */
export type RosterState =
  | { readonly kind: "loading" }
  | { readonly kind: "ready"; readonly members: readonly TeamLoadEntry[] }
  | { readonly kind: "empty" }
  | { readonly kind: "error"; readonly error: ApiError };

/** The settled answer, kept for the life of the page. */
let cached: RosterState | null = null;

/** The read in flight, so concurrent pickers share one request. */
let inFlight: Promise<RosterState> | null = null;

/**
 * Reads the roster, from cache when it has already answered.
 *
 * Never rejects: a failure arrives as the `error` arm carrying the Spanish copy
 * the panel renders, so a picker cannot take an island down by opening.
 */
export async function readRoster(): Promise<RosterState> {
  if (cached !== null) return cached;
  inFlight ??= fetchRoster();
  try {
    return await inFlight;
  } finally {
    inFlight = null;
  }
}

async function fetchRoster(): Promise<RosterState> {
  const result = await getTeamLoad();
  if (!result.ok) {
    // Deliberately not cached: the next open should try again.
    return { kind: "error", error: result.error };
  }
  const members = result.data.items;
  cached =
    members.length === 0 ? { kind: "empty" } : { kind: "ready", members };
  return cached;
}

/**
 * Drops the cached roster.
 *
 * Called after an assignment lands, because the load figures the picker shows
 * beside each name — open tasks, overloaded or not — have just changed for two
 * people.
 */
export function invalidateRoster(): void {
  cached = null;
}
