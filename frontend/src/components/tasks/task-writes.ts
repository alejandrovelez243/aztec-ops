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

import { deleteTask, patchTask } from "../../lib/api/client";
import { invalidateRoster } from "../../lib/team/roster";
import { dayLongLabel, parseISODay } from "../../lib/ui/calendar";
import { toast } from "../../lib/toast";
import { confirmDestructive } from "../ui/confirm-dialog";
import { renderDateControl } from "../ui/date-field";
import { failureCopy } from "../projects/messages";
import { renderOwnerControl } from "../projects/owner-menu";
import { renderPriorityControl } from "../projects/priority-menu";
import { multiValues } from "../ui/multi-select";
import {
  dependencyValue,
  renderDependencyField,
  restoreDependencyField,
} from "./dependency-field";

/** What a reassignment tells the operator when nobody owns the task now. */
const UNOWNED = "La tarea quedó sin responsable.";

/** What clearing a due date tells the operator. */
const UNDATED = "La tarea quedó sin fecha de vencimiento.";

/** What an emptied prerequisite set tells the operator. */
const UNBLOCKED = "La tarea ya no espera a ninguna otra.";

/**
 * What the removal dialog asks, in the operator's terms.
 *
 * It leads with what is *kept*, because that is the question somebody about to
 * press a red button actually has. "Se puede deshacer" is a promise this build
 * keeps literally: the toast carries the undo.
 */
const REMOVAL_BODY =
  "La tarea sale de la lista del proyecto. Sus bloqueos, sus comentarios y las " +
  "dependencias que la nombran se conservan, y la acción se puede deshacer.";

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
  // `is_overdue` is derived from the very date that just moved, so the lateness
  // flag has to move with it — otherwise a task pushed into next week keeps
  // wearing "vencida" until something else happens to re-read it. Scoped to the
  // row or header this control sits in, so the one function serves the table and
  // the task's own screen without either knowing about the other.
  const scope = control.closest("[data-task-row], [data-task-header]");
  const overdue = scope?.querySelector<HTMLElement>("[data-field='overdue']");
  if (overdue !== null && overdue !== undefined) {
    overdue.hidden = !result.data.is_overdue;
  }
  toast({
    kind: "success",
    title: "Fecha actualizada",
    detail:
      result.data.due_date === null || result.data.due_date === undefined
        ? UNDATED
        : `La tarea vence el ${longDay(result.data.due_date)}.`,
  });
}

/**
 * Re-prioritises one task and repaints the chip that asked.
 *
 * The paint comes from the response, never from the clicked option: `PATCH`
 * returns the task re-read after the write, so the label and the colour on
 * screen are the ones the catalog holds rather than the ones this build had
 * rendered into the panel. That matters more here than anywhere else, because a
 * priority renamed or recoloured from the admin is exactly the change a picker
 * painted from its own options would hide.
 *
 * `priority` travels as a bare code, and only when it changed — the menu
 * suppresses re-choosing the current one, so no PATCH reaches here that the
 * timeline would record as a reprioritisation that moved nothing.
 *
 * @param control - The `[data-priority-control]` that was chosen from; carries
 *   the task code in `data-code`.
 * @param priorityCode - `catalog.Priority.code` of the chosen priority.
 */
export async function submitTaskPriority(
  control: HTMLElement,
  priorityCode: string,
): Promise<void> {
  const code = control.dataset["code"] ?? "";
  if (code === "") return;

  const result = await patchTask(code, { priority: priorityCode });
  if (!result.ok) {
    const copy = failureCopy(result.error);
    toast({ kind: "error", title: copy.title, detail: copy.detail });
    return;
  }

  const priority = result.data.priority;
  renderPriorityControl(control, priority);
  toast({
    kind: "success",
    title: "Prioridad actualizada",
    detail: `La tarea quedó en ${priority.label}.`,
  });
}

/**
 * Redraws one task's prerequisites, and puts the chips back if the server refuses.
 *
 * The whole array travels on every save, `[]` included: `depends_on` replaces the
 * set and an absent key means "leave alone", so a task is only ever unblocked by
 * sending the empty list. A chip the operator did not touch therefore has to be
 * re-sent, which is why the request is read off the control rather than
 * assembled from the one chip that moved.
 *
 * Two refusals are expected here rather than exceptional, and both are the
 * server's alone to detect. A prerequisite that would close a loop is answered
 * `409 conflicting_state` with the chain in `details.cycle`; one naming a task of
 * another project is a `422` blaming `depends_on`. Neither is guessed at in the
 * browser — this build cannot see the project's whole graph — so the control
 * moves optimistically and is put back from the last accepted set, with the
 * refusal named. Chips left showing a set the database rejected would be the
 * screen lying, and the next save would send the lie back as a deliberate edit.
 *
 * @param control - The `[data-multi-select]` holding the prerequisites.
 * @param code - The task being edited; the control belongs to a header, not to a
 *   row, so it carries no code of its own.
 */
export async function submitTaskDependencies(
  control: HTMLElement,
  code: string,
): Promise<void> {
  if (code === "") return;

  control.setAttribute("aria-busy", "true");
  const result = await patchTask(code, {
    depends_on: [...multiValues(control)],
  });
  control.setAttribute("aria-busy", "false");

  if (!result.ok) {
    restoreDependencyField(control);
    const copy = failureCopy(result.error);
    toast({ kind: "error", title: copy.title, detail: copy.detail });
    return;
  }

  renderDependencyField(control, result.data.dependencies);
  const names = result.data.dependencies.map(dependencyValue);
  toast({
    kind: "success",
    title: "Dependencias actualizadas",
    detail:
      names.length === 0 ? UNBLOCKED : `Ahora espera a ${names.join(", ")}.`,
  });
}

/**
 * Removes one task, and offers to put it back.
 *
 * The operator's verb is "eliminar" everywhere the operator can see it — the menu
 * item, this dialog, this toast — while the mechanism stays honest in the body
 * copy: the row is flagged `is_archived`, so the blockers raised against it, the
 * comments written on it and every dependency edge naming it survive. Naming what
 * survives is the operator's real question, and it is also what makes the undo
 * credible. "Archivar" is never surfaced (`delete_member`'s convention).
 *
 * The undo is an ordinary edit, not a re-creation: `PATCH { is_archived: false }`
 * on the same code. It rides the toast, so it lasts as long as the toast does —
 * acceptable precisely because nothing was actually destroyed, and the task is
 * still reachable by code afterwards.
 *
 * Both the confirmation and the removal repaint through `onDone`, not from here:
 * this function knows a task code, and only the caller knows whether it is
 * holding a table row or a whole screen.
 *
 * @param code - The task to remove.
 * @param onDone - Re-reads and repaints whatever the caller has on screen. Called
 *   after a successful removal and again after a successful undo; a rejection
 *   inside it is the caller's to handle.
 * @returns Whether the task was removed. `false` covers a cancelled dialog and a
 *   refused request alike — in both, nothing on screen should move.
 */
export async function submitTaskRemoval(
  code: string,
  onDone: () => Promise<void>,
): Promise<boolean> {
  if (code === "") return false;

  const confirmed = await confirmDestructive({
    title: `Eliminar ${code}`,
    body: REMOVAL_BODY,
    confirmLabel: "Eliminar tarea",
  });
  if (!confirmed) return false;

  const result = await deleteTask(code);
  if (!result.ok) {
    const copy = failureCopy(result.error);
    toast({ kind: "error", title: copy.title, detail: copy.detail });
    return false;
  }

  await onDone();
  toast({
    kind: "success",
    title: "Tarea eliminada",
    detail: `${code} salió de la lista. Sus bloqueos y comentarios siguen ahí.`,
    action: {
      label: "Deshacer",
      onSelect: () => {
        void restoreTask(code, onDone);
      },
    },
  });
  return true;
}

/**
 * Puts a removed task back, from the toast that announced its removal.
 *
 * Separate from the press handler because it is asynchronous and the handler is
 * not: a failed restore has to say so, and a `void`ed promise inside the toast
 * would swallow it. A restore that fails leaves the list exactly as the removal
 * left it, which is why the copy names the code — the operator can still find it.
 */
async function restoreTask(
  code: string,
  onDone: () => Promise<void>,
): Promise<void> {
  const result = await patchTask(code, { is_archived: false });
  if (!result.ok) {
    const copy = failureCopy(result.error);
    toast({ kind: "error", title: copy.title, detail: copy.detail });
    return;
  }
  await onDone();
  toast({
    kind: "success",
    title: "Tarea restaurada",
    detail: `${code} volvió a la lista.`,
  });
}

/** "28 de julio de 2026", for the sentence a save confirms itself with. */
function longDay(iso: string): string {
  const day = parseISODay(iso);
  return day === null ? iso : dayLongLabel(day);
}
