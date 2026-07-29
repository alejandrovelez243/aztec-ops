/**
 * The writes one task accepts, shared by every screen that offers them.
 *
 * The project's task table and the task's own page render the same controls
 * against the same aggregate, so the write behind a control lives here rather
 * than in either island: two copies would drift on the day one of them starts
 * invalidating the roster and the other does not, and the operator would meet a
 * reassignment that updates the load figures on one screen and not the other.
 *
 * **Every export is a pure function of its arguments.** A component's `<script>`
 * module is evaluated once per app, not once per page (`components/projects/
 * island.ts`), so a module-level `Map` or `AbortController` here would outlive
 * the screen that filled it and leak into the next navigation. State belongs to
 * the mount that owns it.
 *
 * State is deliberately absent from this file: a move is not a field write. It
 * goes through `POST /api/v1/tasks/{code}/transition`, which validates against
 * `WorkflowTransition`, and its pending/confirm dance belongs to the island
 * holding the move in flight (CLAUDE.md rule 2).
 */

import { patchTask } from "../../lib/api/client";
import { invalidateRoster } from "../../lib/team/roster";
import { dayLongLabel, parseISODay } from "../../lib/ui/calendar";
import { toast } from "../../lib/toast";
import { renderDateControl } from "../ui/date-field";
import { failureCopy } from "../projects/messages";
import { renderOwnerControl } from "../projects/owner-menu";

/** What a reassignment tells the operator when nobody owns the task now. */
const UNOWNED = "La tarea quedó sin responsable.";

/** What clearing a due date tells the operator. */
const UNDATED = "La tarea quedó sin fecha de vencimiento.";

/**
 * Reassigns one task and repaints the control that asked.
 *
 * The paint comes from the response, never from the chosen option: `PATCH`
 * returns the task re-read after the write, so the name on screen is the one the
 * database holds rather than the one the operator clicked. That is also why this
 * does not wait for the `task.updated` envelope — the write already answered,
 * and holding the control pending until the stream agrees would leave it spinning
 * on a screen whose connection is down.
 *
 * @param control - The `[data-owner-control]` that was chosen from; carries the
 *   task code in `data-code`.
 * @param alias - `accounts.User.code` of the new owner, or `null` to unassign.
 *   `null` is sent explicitly: an absent key means "leave alone", so omitting it
 *   would make unassigning impossible.
 */
export async function submitTaskOwner(
  control: HTMLElement,
  alias: string | null,
): Promise<void> {
  const code = control.dataset.code ?? "";
  if (code === "") return;

  const result = await patchTask(code, { assignee: alias });
  if (!result.ok) {
    const copy = failureCopy(result.error);
    toast({ kind: "error", title: copy.title, detail: copy.detail });
    return;
  }

  const owner = result.data.assignee ?? null;
  renderOwnerControl(control, owner);
  // Two people's open-task counts just moved, and the picker reports them
  // beside every name it offers.
  invalidateRoster();
  toast({
    kind: "success",
    title: "Responsable actualizado",
    detail: owner === null ? UNOWNED : `Ahora responde ${owner.label}.`,
  });
}

/**
 * Moves one task's due date, or clears it.
 *
 * `null` travels explicitly, because an absent key means "leave alone" — so
 * omitting it would make "sin fecha" unreachable, which is the one value the
 * risk engine most wants to be able to see (`HasNoTargetDate`'s task twin).
 *
 * The chip is repainted from the response rather than from the chosen day: the
 * server also recomputes `is_overdue` against its own clock, and a chip painted
 * from the click would disagree with the row's own "vencida" flag.
 *
 * @param control - The `[data-date-control]` that was picked from; carries the
 *   task code in `data-code`.
 * @param value - `YYYY-MM-DD`, or `null` to leave the task undated.
 */
export async function submitTaskDueDate(
  control: HTMLElement,
  value: string | null,
): Promise<void> {
  const code = control.dataset["code"] ?? "";
  if (code === "") return;

  const result = await patchTask(code, { due_date: value });
  if (!result.ok) {
    const copy = failureCopy(result.error);
    toast({ kind: "error", title: copy.title, detail: copy.detail });
    return;
  }

  renderDateControl(control, result.data.due_date ?? null);
  toast({
    kind: "success",
    title: "Fecha actualizada",
    detail:
      result.data.due_date === null || result.data.due_date === undefined
        ? UNDATED
        : `La tarea vence el ${longDay(result.data.due_date)}.`,
  });
}

/** "28 de julio de 2026", for the sentence a save confirms itself with. */
function longDay(iso: string): string {
  const day = parseISODay(iso);
  return day === null ? iso : dayLongLabel(day);
}
