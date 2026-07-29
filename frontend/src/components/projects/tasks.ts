/**
 * Keeps the task table in step with the bus.
 *
 * Task topics carry state *codes*, never the label or the colour a chip needs
 * (`docs/EVENTS.md` §4), and a task's lateness is derived from the server's
 * clock. So an envelope is treated as a notification — "something about this
 * project's work changed" — and the authoritative values come from re-reading
 * the list. What the envelope decides is *whether* to re-read, which is what
 * keeps this off a polling timer.
 *
 * Rows are patched in place and new ones are appended; nothing is removed,
 * because the API has no task deletion and a row vanishing under an operator's
 * cursor would be a bug, not a feature.
 */

import { getProjectTasks } from "../../lib/api/client";
import { subscribe } from "../../lib/stream/store";
import type { Topic } from "../../lib/stream/topics";
import {
  applyTone,
  cloneTemplate,
  isFresher,
  pulse,
  readString,
  setField,
} from "./dom";
import { dueState } from "./format";
import { markEvent } from "./stale";
import { semanticTone, stateTone } from "./tone";
import type { TaskItem } from "../../lib/api/domain";

/** Topics that can change something a task row shows. */
const WATCHED: readonly Topic[] = [
  "task.created",
  "task.updated",
  "task.state_changed",
];

/**
 * Mounts the task table's live half.
 *
 * @param root - The element carrying `data-tasks`.
 * @returns The teardown; drops every subscription.
 */
export function mountTasks(root: HTMLElement): () => void {
  const code = root.dataset.projectCode ?? "";
  let refreshing = false;

  const offs = WATCHED.map((topic) =>
    subscribe(topic, (envelope) => {
      if (readString(envelope.payload, "project_code") !== code) return;
      markEvent(envelope.occurred_at);
      if (refreshing) return;
      refreshing = true;
      void refresh(root, code, envelope.occurred_at).finally(() => {
        refreshing = false;
      });
    }),
  );

  return () => {
    for (const off of offs) off();
  };
}

/** Re-reads the tasks and patches the rows that changed. */
async function refresh(
  root: HTMLElement,
  code: string,
  occurredAt: string,
): Promise<void> {
  const result = await getProjectTasks(code);
  if (!result.ok) return;

  const body = root.querySelector<HTMLElement>("[data-task-rows]");
  if (body === null) return;

  for (const task of result.data.items) {
    const existing = body.querySelector<HTMLElement>(
      `[data-task-row][data-code="${CSS.escape(task.code)}"]`,
    );
    if (existing !== null) {
      if (!isFresher(occurredAt, existing.dataset.updatedAt)) continue;
      patchRow(existing, task, occurredAt);
      pulse(existing);
      continue;
    }
    const row = cloneTemplate(root, "task");
    if (row === null) continue;
    row.dataset.code = task.code;
    setField(row, "task-code", task.code);
    patchRow(row, task, occurredAt);
    body.appendChild(row);
    pulse(row);
  }

  const count = result.data.count;
  const tabs = document.querySelector<HTMLElement>("[data-tabs]");
  if (tabs !== null) setField(tabs, "tasks-count", String(count));

  const empty = root.querySelector<HTMLElement>("[data-tasks-empty]");
  const wrap = root.querySelector<HTMLElement>("[data-tasks-wrap]");
  if (empty !== null) empty.hidden = count > 0;
  if (wrap !== null) wrap.hidden = count === 0;
}

/** Writes one task's current values into its row. */
function patchRow(row: HTMLElement, task: TaskItem, occurredAt: string): void {
  setField(row, "task-title", task.title);

  const chip = row.querySelector<HTMLElement>("[data-state-chip]");
  if (chip !== null) {
    applyTone(chip, stateTone(task.state));
    setField(chip, "state-label", task.state.label);
    chip.dataset.category = task.state.category;
  }

  const dueChip = row.querySelector<HTMLElement>("[data-due-chip]");
  if (dueChip !== null) {
    const due = dueState(task.due_date ?? null);
    applyTone(dueChip, semanticTone(due.tone));
    setField(dueChip, "due-label", due.label);
    if ("title" in due) dueChip.setAttribute("title", due.title);
    else dueChip.removeAttribute("title");
  }

  const overdue = row.querySelector<HTMLElement>("[data-field='overdue']");
  if (overdue !== null) overdue.hidden = !task.is_overdue;

  const owner = row.querySelector<HTMLElement>("[data-field='task-owner']");
  if (owner !== null) owner.textContent = task.assignee?.label ?? "Sin responsable";

  patchDependencies(row, task);
  row.dataset.updatedAt = occurredAt;
}

/**
 * Rewrites the dependency chips.
 *
 * `task_code` when the reference resolved, the operation's own words otherwise
 * — dropping an unresolved label would delete the note somebody actually wrote.
 */
function patchDependencies(row: HTMLElement, task: TaskItem): void {
  const cell = row.querySelector<HTMLElement>("[data-field='dependencies']");
  if (cell === null) return;
  cell.replaceChildren();

  if (task.dependencies.length === 0) {
    const dash = document.createElement("span");
    dash.className = "small";
    dash.textContent = "—";
    cell.appendChild(dash);
    return;
  }
  for (const dependency of task.dependencies) {
    const chip = document.createElement("span");
    chip.className = "chip chip-code dep";
    chip.title = dependency.raw_label;
    chip.textContent = dependency.task_code ?? dependency.raw_label;
    cell.appendChild(chip);
  }
}
