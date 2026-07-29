/**
 * A task's legal moves — read from the API, never inferred from its state.
 *
 * The rule this file exists to keep: **the client does not know what is legal.**
 * Legality lives in `WorkflowTransition` rows, in guards and in `requires_fields`
 * (`docs/API.md` §2.2), which is why the project's buttons come from
 * `project.transitions` and from nothing else. A task deserves the same
 * treatment, and a picker listing "every state of the task workflow" would be
 * the exact opposite of it: `GET /workflows` publishes the columns of a graph
 * and says, in as many words, that it publishes no edge.
 *
 * Today `TaskView` does not carry `transitions`. So this reads the field at
 * runtime instead of asserting it into the generated tree — the same defensive
 * narrowing `dom.ts` applies to envelope payloads, and for the same reason: a
 * shape that grew must not be able to take a surface down. Two consequences,
 * both deliberate.
 *
 * 1. While the field is absent, every row's move control renders **visibly
 *    dead** with the reason named, rather than offering a guess.
 * 2. On the build where the backend starts sending `transitions` on `TaskView`,
 *    the control turns on with no further frontend change — and `npm run gen:api`
 *    is what should then replace this reader with the generated type.
 */

import { isRecord } from "./dom";
import type { TaskItem } from "../../lib/api/domain";

/** One edge out of a task's current state, as a row's menu renders it. */
export interface TaskMove {
  /** `WorkflowState.code` of the destination; the body of the POST. */
  readonly toStateCode: string;
  /** The operator's own Spanish wording for the move. */
  readonly label: string;
  /** Whether the edge demands a motive before it may be submitted. */
  readonly requiresReason: boolean;
}

/**
 * The legal moves the API published for this task.
 *
 * @param task - One task as `GET /projects/{code}/tasks` returned it.
 * @returns The edges, or an empty list when the payload carries none — which
 *   covers both "this state is terminal" and "the field is not published yet".
 *   Both render the same way: no menu, and the state chip alone.
 */
export function taskMoves(task: TaskItem): readonly TaskMove[] {
  const payload: unknown = task;
  if (!isRecord(payload)) return [];
  const published = payload["transitions"];
  if (!Array.isArray(published)) return [];

  const moves: TaskMove[] = [];
  for (const entry of published as readonly unknown[]) {
    const move = toMove(entry);
    if (move !== null) moves.push(move);
  }
  return moves;
}

/** Narrows one published edge; anything malformed is dropped, never guessed at. */
function toMove(entry: unknown): TaskMove | null {
  if (!isRecord(entry)) return null;
  const toState = entry["to_state"];
  if (!isRecord(toState)) return null;
  const code = toState["code"];
  const label = entry["label"];
  if (typeof code !== "string" || code === "") return null;
  if (typeof label !== "string" || label === "") return null;
  return {
    toStateCode: code,
    label,
    requiresReason: entry["requires_reason"] === true,
  };
}
