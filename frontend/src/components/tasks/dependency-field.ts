/**
 * The task header's prerequisite picker, seen from the code that has to keep it
 * true.
 *
 * One painter for a set that three things move: the operator editing it here,
 * the server answering the `PATCH`, and a colleague's edit arriving as
 * `task.updated`. Written per caller, the three would drift, and the first thing
 * to diverge is what a *prose* prerequisite looks like — the API resolves a
 * dependency to a task code when it can and keeps the operation's own words when
 * it cannot, so the same set has two renderings and only one of them is a code.
 *
 * **A prerequisite's wire value is what {@link dependencyLabel} shows**: the task
 * code when the reference resolved, the raw label when it did not.
 * `PATCH /api/v1/tasks/{code}` reads every entry of `depends_on` exactly as
 * `POST .../tasks` does — a task code of this project becomes an edge, anything
 * else is kept verbatim as `raw_label` — so re-sending the chips round-trips the
 * prose edges the importer created instead of deleting them
 * (`backend/apps/work/api/schemas.py`, `TaskUpdateIn`).
 *
 * **The last accepted set lives on the control**, in `data-committed`, because a
 * refusal has to be undone: the chips move the moment they are clicked, and a
 * `409` two hundred milliseconds later would otherwise leave the screen showing
 * a set the database never accepted. Module-level state cannot hold it — an
 * island script is evaluated once per app, not once per page.
 */

import type { DependencyRef } from "../../lib/api/domain";
import {
  findMultiSelect,
  multiValues,
  setMultiValues,
} from "../ui/multi-select";
import { dependencyLabel } from "./presentation";

/** The facet of the task header's dependency picker; the markup's side of the pact. */
export const DEPENDS_ON_FACET = "task-depends-on";

/** Where the last set the server accepted is parked, on the control itself. */
const COMMITTED = "committed";

/** The picker inside `root`, or `null` when the header renders the read-only arm. */
export function findDependencyField(root: ParentNode): HTMLElement | null {
  return findMultiSelect(root, DEPENDS_ON_FACET);
}

/** What one prerequisite is sent back as; see this module's note on prose edges. */
export function dependencyValue(dependency: DependencyRef): string {
  return dependencyLabel(dependency);
}

/**
 * Paints an authoritative set of prerequisites and records it as the last good one.
 *
 * @param control - The `[data-multi-select]` this header renders.
 * @param dependencies - The set from a response, never from what was clicked:
 *   the server decides which entries resolved to a task and which stayed prose,
 *   and a control painted from the click would show a code the database does not
 *   have.
 */
export function renderDependencyField(
  control: HTMLElement,
  dependencies: readonly DependencyRef[],
): void {
  const values = dependencies.map(dependencyValue);
  setMultiValues(control, values);
  control.dataset[COMMITTED] = serialize(values);
}

/**
 * Takes the set the server first rendered as the last good one.
 *
 * Called at mount rather than at first paint, because there is no first paint:
 * the chips are server-rendered, so the DOM *is* the last accepted set until a
 * write says otherwise. Skipped when a set is already recorded, so a re-mount
 * after a navigation cannot promote a half-finished edit to "accepted".
 */
export function rememberDependencies(control: HTMLElement): void {
  if (control.dataset[COMMITTED] !== undefined) return;
  control.dataset[COMMITTED] = serialize(multiValues(control));
}

/**
 * Puts the chips back to the last set the server accepted.
 *
 * The answer to a refused write. Leaving the operator's chips on screen after a
 * `409` would be the interface asserting something the database denied — and the
 * next save would send that fiction as if it were a deliberate edit.
 */
export function restoreDependencyField(control: HTMLElement): void {
  setMultiValues(control, committedDependencies(control));
}

/** The last accepted set, `[]` when nothing was ever recorded or the value is unreadable. */
function committedDependencies(control: HTMLElement): readonly string[] {
  const raw = control.dataset[COMMITTED] ?? "";
  if (raw === "") return [];
  const parsed = parseJson(raw);
  if (!Array.isArray(parsed)) return [];
  return parsed.filter((entry): entry is string => typeof entry === "string");
}

/** JSON, because a prerequisite's prose can contain any separator we might pick. */
function serialize(values: readonly string[]): string {
  return JSON.stringify(values);
}

/**
 * Reads back what {@link serialize} wrote, answering `null` to anything else.
 *
 * A hand-edited attribute is not worth a thrown exception on a screen that is
 * otherwise correct: the honest recovery is "no set recorded", which restores to
 * empty and is visibly wrong rather than invisibly wrong.
 */
function parseJson(raw: string): unknown {
  try {
    return JSON.parse(raw);
  } catch {
    return null;
  }
}
