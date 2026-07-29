/**
 * Painting one authoritative task read across the task screen.
 *
 * Every writer and every listener on this surface ends the same way: it holds a
 * `TaskDetailView` the server produced *after* the change and has to make the
 * screen agree with it. That job lives here once — written per island, the
 * transition bar and the live listener would drift, and the first thing to
 * diverge is the pair nobody re-checks: the state chip and the buttons legal
 * from it.
 *
 * Field-level patches against the rendered DOM, never a rebuild of the region:
 * the operator may be halfway through the comment box, and replacing the screen
 * would take it away from them.
 *
 * The transition list is the one part that *is* rebuilt, because it is not a
 * value but a set — the moves legal from the new state have no relation to the
 * buttons legal from the old one.
 */

import { applyTone, cloneTemplate, pulse, setField } from "../projects/dom";
import { renderOwnerControl } from "../projects/owner-menu";
import { renderDateControl } from "../ui/date-field";
import { joinFields } from "../projects/messages";
import { stateTone, taxonomyTone } from "../projects/tone";
import {
  dependencyLabel,
  dependencyTitle,
  emptyTaskFields,
} from "./presentation";
import type { ActorRef, TaskDetail } from "../../lib/api/domain";

/**
 * Paints a task read across the header and its transition bar.
 *
 * @param task - The server's answer; the only source of every value written.
 *   Nothing here reads the DOM to decide what to render.
 */
export function applyTaskDetail(task: TaskDetail): void {
  const header = document.querySelector<HTMLElement>("[data-task-header]");
  if (header !== null) {
    const stateChip = header.querySelector<HTMLElement>("[data-state-chip]");
    if (stateChip !== null) {
      applyTone(stateChip, stateTone(task.state));
      setField(stateChip, "state-label", task.state.label);
      stateChip.dataset.category = task.state.category;
    }

    const priority = header.querySelector<HTMLElement>("[data-priority-chip]");
    if (priority !== null) {
      applyTone(priority, taxonomyTone(task.priority.color));
      setField(priority, "priority-label", task.priority.label);
    }

    const overdue = header.querySelector<HTMLElement>("[data-field='overdue']");
    if (overdue !== null) overdue.hidden = !task.is_overdue;

    // The due date is a control now, not a chip: one painter, so a date this
    // screen set and one a colleague set leave it reading the same thing.
    const dueControl = header.querySelector<HTMLElement>("[data-date-control]");
    if (dueControl !== null) {
      renderDateControl(dueControl, task.due_date ?? null);
    }

    setField(header, "task-title", task.title);
    renderDetailLine(header, task.detail);
    renderLastProgress(header, task.last_progress);
    renderOwner(header, task.assignee ?? null);
    renderDependencies(header, task);

    header.dataset.updatedAt = task.updated_at;
    pulse(header);
  }

  const bar = document.querySelector<HTMLElement>("[data-task-transition-bar]");
  if (bar === null) return;
  rebuildOptions(bar, task);
  bar.dataset.updatedAt = task.updated_at;
}

/** Writes the description, hiding the line entirely when there is none. */
function renderDetailLine(header: HTMLElement, detail: string): void {
  const line = header.querySelector<HTMLElement>("[data-task-detail-line]");
  if (line === null) return;
  line.textContent = detail;
  line.hidden = detail === "";
}

/**
 * Writes the recorded advance into whichever arm it belongs.
 *
 * `data-filled` is the field's single mode flag: the sentence and the ámbar
 * "sin avance" are two renderings of one value, so they are never both shown
 * and never both hidden (Present-Absence Rule).
 */
function renderLastProgress(header: HTMLElement, lastProgress: string): void {
  const field = header.querySelector<HTMLElement>("[data-last-progress]");
  if (field === null) return;
  field.dataset.filled = lastProgress === "" ? "false" : "true";
  setField(field, "last-progress", lastProgress);
}

/**
 * Renders whoever carries the task now, or the named absence.
 *
 * Delegated to `renderOwnerControl`, the same painter the project header and
 * every task row use, because the owner control here is the same component:
 * the badge *is* the trigger, so a reassignment that arrives over the stream
 * and one the operator just made must leave the control in the same state. A
 * second painter written here is how the two start disagreeing about what
 * "unowned" looks like.
 */
function renderOwner(header: HTMLElement, actor: ActorRef | null): void {
  const control = header.querySelector<HTMLElement>("[data-owner-control]");
  if (control === null) return;
  renderOwnerControl(control, actor);
}

/** Rebuilds the prerequisite chips, keeping the operation's own words. */
function renderDependencies(header: HTMLElement, task: TaskDetail): void {
  const list = header.querySelector<HTMLElement>("[data-dependency-list]");
  if (list === null) return;
  list.replaceChildren();

  if (task.dependencies.length === 0) {
    const none = cloneTemplate(header, "no-dependency");
    if (none !== null) list.appendChild(none);
    return;
  }

  for (const dependency of task.dependencies) {
    const chip = cloneTemplate(header, "dependency");
    if (chip === null) continue;
    chip.textContent = dependencyLabel(dependency);
    const title = dependencyTitle(dependency);
    if (title !== undefined) chip.title = title;
    list.appendChild(chip);
  }
}

/** Rebuilds the transition buttons from the fresh list of legal moves. */
function rebuildOptions(bar: HTMLElement, task: TaskDetail): void {
  const list = bar.querySelector<HTMLElement>("[data-transition-options]");
  if (list === null) return;

  const empty = new Set(emptyTaskFields(task));
  list.replaceChildren();

  for (const transition of task.transitions) {
    const option = cloneTemplate(bar, "transition");
    if (option === null) continue;
    const button = option.querySelector<HTMLButtonElement>(
      "[data-task-transition]",
    );
    const missingLine = option.querySelector<HTMLElement>(
      "[data-field='transition-missing']",
    );
    if (button === null) continue;

    button.dataset.toState = transition.to_state.code;
    button.dataset.requiresReason = String(transition.requires_reason);
    setField(option, "transition-label", transition.label);

    const missing = transition.requires_fields.filter((field) =>
      empty.has(field),
    );
    if (missing.length > 0 && missingLine !== null) {
      const id = `req-${task.code}-${transition.to_state.code}`;
      button.disabled = true;
      button.setAttribute("aria-describedby", id);
      missingLine.id = id;
      missingLine.hidden = false;
      missingLine.textContent = `Falta ${joinFields(missing)}`;
    }
    list.appendChild(option);
  }

  const terminal = bar.querySelector<HTMLElement>("[data-no-transitions]");
  if (terminal !== null) terminal.hidden = task.transitions.length > 0;
}
