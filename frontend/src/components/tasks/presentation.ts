/**
 * Pure readings of one task, shared by the server render and the islands.
 *
 * Framework-free and side-effect-free on purpose: the first paint and every
 * live patch answer the same questions — which fields a transition is waiting
 * for, where this task's project lives — and two implementations of that would
 * eventually disagree on the same task, which is how a button starts being
 * enabled in one arm and dead in the other.
 *
 * All output is Spanish (CLAUDE.md §Language); every identifier is English.
 */

import type { DependencyRef, TaskDetail } from "../../lib/api/domain";

/**
 * Separator the backend mints task codes with: `PRJ-01-T03`
 * (`backend/apps/work/models.py`, `TASK_CODE_INFIX`).
 */
const TASK_CODE_INFIX = "-T";

/**
 * The project a task code belongs to, read from the code itself.
 *
 * Used in exactly one place: the arm where the task could **not** be read, so
 * `task.project.code` — the authoritative answer everywhere else — does not
 * exist. A dead link still deserves a way back to the wall it came from, and
 * the code's shape is minted by the server rather than assembled here.
 *
 * @returns The project's code, or `null` when the string is not a task code, in
 *   which case the caller falls back to the project list rather than inventing
 *   a route.
 */
export function projectCodeOf(taskCode: string): string | null {
  const cut = taskCode.lastIndexOf(TASK_CODE_INFIX);
  return cut <= 0 ? null : taskCode.slice(0, cut);
}

/** Where one project's own screen lives. */
export function projectHref(code: string): string {
  return `/projects/${encodeURIComponent(code)}`;
}

/**
 * The task attributes a transition's `requires_fields` can name, and whether
 * each is currently empty.
 *
 * A transition demanding something the task has not got renders **visibly
 * dead** with the field named, which is the whole reason the API ships
 * `requires_fields` even though it changes nothing about the request body.
 *
 * An attribute this build does not know counts as **empty**: the honest answer
 * to a contract that grew is a button that refuses and says which field it is
 * waiting for, not a button that posts and is refused by the server.
 */
export function emptyTaskFields(task: TaskDetail): readonly string[] {
  const values: Readonly<Record<string, unknown>> = {
    title: task.title,
    detail: task.detail,
    last_progress: task.last_progress,
    priority: task.priority,
    assignee: task.assignee ?? null,
    due_date: task.due_date ?? null,
    // An empty list is an absence, and `[]` is neither `null` nor `""`; folded
    // here so the emptiness test below stays one comparison for every field.
    depends_on: task.dependencies.length === 0 ? null : task.dependencies,
  };

  const empty = new Set<string>();
  for (const transition of task.transitions) {
    for (const field of transition.requires_fields) {
      const value = values[field];
      if (value === undefined || value === null || value === "") {
        empty.add(field);
      }
    }
  }
  return [...empty];
}

/**
 * How one prerequisite reads on a chip: the task's code when the reference
 * resolved, the operation's own words when it did not.
 *
 * The unresolved case is the normal one — most source tasks describe what they
 * are waiting on in prose — so it is rendered as written rather than dropped.
 */
export function dependencyLabel(dependency: DependencyRef): string {
  const code = dependency.task_code ?? "";
  return code === "" ? dependency.raw_label : code;
}

/**
 * The prose behind a resolved prerequisite's code, as its `title`.
 *
 * `undefined` when there is nothing to add — a reference that resolved carries
 * no `raw_label`, and a `title` equal to the chip's own text is a tooltip that
 * repeats what is already on screen.
 */
export function dependencyTitle(dependency: DependencyRef): string | undefined {
  const raw = dependency.raw_label;
  if (raw === "" || raw === dependencyLabel(dependency)) return undefined;
  return raw;
}
