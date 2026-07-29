/**
 * The task table's live half, and the writes its rows can perform.
 *
 * **Reading.** Task topics carry state *codes*, never the label or the colour a
 * chip needs (`docs/EVENTS.md` §4), and a task's lateness is derived from the
 * server's clock. So an envelope is treated as a notification — "something about
 * this project's work changed" — and the authoritative values come from
 * re-reading the list. What the envelope decides is *whether* to re-read, which
 * is what keeps this off a polling timer.
 *
 * **Writing.** A move is submitted and then *waited for*. Nothing on the row is
 * repainted until the `task.state_changed` envelope for that task arrives, or —
 * if the stream is down — until {@link STREAM_CONFIRM_MS} has passed and the
 * POST's own response is painted instead. Legality lives on the server; a chip
 * flipped ahead of it and rolled back afterwards is worse than a spinner
 * (`docs/standards/PATTERNS_FRONTEND.md` §8). This is the same discipline the
 * project's transition bar keeps, for the same reason.
 *
 * Rows are patched in place and new ones are appended; nothing is removed,
 * because the API has no task deletion and a row vanishing under an operator's
 * cursor would be a bug, not a feature.
 */

import {
  getProjectTasks,
  postProjectTask,
  postTaskTransition,
} from "../../lib/api/client";
import { shake } from "../../lib/motion/spring";
import { subscribe } from "../../lib/stream/store";
import { toast } from "../../lib/toast";
import type { Topic } from "../../lib/stream/topics";
import {
  applyTone,
  cloneTemplate,
  isFresher,
  pulse,
  readString,
  setField,
  setPending,
} from "./dom";
import { mountMenus } from "../../lib/ui/menu";
import {
  CREATE_INCOMPLETE,
  failureCopy,
  joinFields,
  TASK_MOVES_UNAVAILABLE,
} from "./messages";
import { mountOwnerMenus, renderOwnerControl } from "./owner-menu";
import { askReason } from "./reason-dialog";
import { markEvent } from "./stale";
import { taskMoves, type TaskMove } from "./task-moves";
import { submitTaskDueDate, submitTaskOwner } from "../tasks/task-writes";
import { mountDateFields, renderDateControl } from "../ui/date-field";
import { stateTone, taxonomyTone } from "./tone";
import type { TaskItem } from "../../lib/api/domain";

/** Topics that can change something a task row shows. */
const WATCHED: readonly Topic[] = [
  "task.created",
  "task.updated",
  "task.state_changed",
];

/** How long a submitted move waits for the stream before trusting the POST. */
const STREAM_CONFIRM_MS = 8_000;

/** A move the server accepted and the stream has not yet confirmed. */
interface PendingMove {
  readonly control: HTMLElement;
  readonly task: TaskItem;
  readonly timer: number;
}

/**
 * Mounts the task table: its subscriptions and its row controls.
 *
 * @param root - The element carrying `data-tasks`.
 * @returns The teardown; drops every subscription, listener and pending timer.
 */
export function mountTasks(root: HTMLElement): () => void {
  const code = root.dataset.projectCode ?? "";
  let refreshing = false;

  /** Moves awaiting confirmation, keyed by task code. */
  const pending = new Map<string, PendingMove>();

  /** Paints the answer for one task and releases its control, once. */
  const settle = (taskCode: string, source: "stream" | "timeout"): void => {
    const move = pending.get(taskCode);
    if (move === undefined) return;
    pending.delete(taskCode);
    window.clearTimeout(move.timer);
    setMovePending(move.control, false);

    if (source === "stream") {
      // The refresh triggered by the same envelope is the authoritative paint;
      // this only names what happened.
      toast({
        kind: "success",
        title: "Tarea actualizada",
        detail: `${move.task.code} quedó en ${move.task.state.label}.`,
      });
      return;
    }
    const row = rowFor(root, taskCode);
    if (row !== null) {
      patchRow(root, row, move.task, new Date().toISOString());
      pulse(row);
    }
    toast({
      kind: "info",
      title: "Movimiento guardado sin confirmación en vivo",
      detail:
        "El servidor aceptó el cambio, pero el stream no lo confirmó. Reconecta para volver a ver los cambios de tus colegas.",
    });
  };

  const offs = WATCHED.map((topic) =>
    subscribe(topic, (envelope) => {
      if (readString(envelope.payload, "project_code") !== code) return;
      markEvent(envelope.occurred_at);
      // `entity.id` of a task topic is the task code (`docs/EVENTS.md` §4).
      if (pending.has(envelope.entity.id)) settle(envelope.entity.id, "stream");
      if (refreshing) return;
      refreshing = true;
      void refresh(root, code, envelope.occurred_at).finally(() => {
        refreshing = false;
      });
    }),
  );

  const offMoves = mountMenus(root, "[data-move-control]", {
    // Options are rendered by the server and rebuilt by `patchMoves` on every
    // re-read, so the panel is already correct when it opens.
    fill: () => Promise.resolve(),
    choose: async (control, item) => {
      const taskCode = control.dataset.code ?? "";
      const toState = item.dataset.toState ?? "";
      if (taskCode === "" || toState === "") return;
      if (pending.has(taskCode)) return;

      const reason =
        item.dataset.requiresReason === "true"
          ? await askReason(moveLabel(item))
          : "";
      // A cancelled dialog is a cancelled move, not a move without a motive.
      if (reason === null) return;

      setMovePending(control, true);
      const result = await postTaskTransition(taskCode, {
        to_state: toState,
        reason,
      });

      if (!result.ok) {
        setMovePending(control, false);
        const trigger = control.querySelector<HTMLButtonElement>(
          "[data-menu-trigger]",
        );
        if (trigger !== null) void shake(trigger);
        const copy = failureCopy(result.error);
        toast({ kind: "error", title: copy.title, detail: copy.detail });
        // A refused move means this row was stale about legality; re-read
        // rather than patch, so the menu matches reality again.
        if (result.error.kind === "transition_not_allowed") {
          void refresh(root, code, new Date().toISOString());
        }
        return;
      }

      pending.set(taskCode, {
        control,
        task: result.data,
        timer: window.setTimeout(() => {
          settle(taskCode, "timeout");
        }, STREAM_CONFIRM_MS),
      });
    },
  });

  const offOwners = mountOwnerMenus(root, submitTaskOwner);
  const offDates = mountDateFields(root, submitTaskDueDate);
  const offCreate = mountCreateTask(root, code);

  return () => {
    for (const off of offs) off();
    offMoves();
    offOwners();
    offDates();
    offCreate();
    for (const move of pending.values()) window.clearTimeout(move.timer);
    pending.clear();
  };
}

/**
 * The destination a chosen option names, for the reason dialog's title.
 *
 * Read from the label node rather than from the button's `textContent`, which
 * also carries the "Pide motivo" hint and would put it in the dialog heading.
 */
function moveLabel(item: HTMLElement): string {
  return (
    item.querySelector<HTMLElement>("[data-field='move-label']")?.textContent ??
    ""
  ).trim();
}

/** The rendered row for one task code, or `null` when it is not on screen. */
function rowFor(root: HTMLElement, taskCode: string): HTMLElement | null {
  return root.querySelector<HTMLElement>(
    `[data-task-row][data-code="${CSS.escape(taskCode)}"]`,
  );
}

/**
 * Marks a row's move control in-flight.
 *
 * The trigger stops accepting input and hosts the spinner beside the chip,
 * which is deliberately **not** repainted: the state on screen stays the state
 * the server last confirmed.
 */
function setMovePending(control: HTMLElement, isPending: boolean): void {
  const trigger = control.querySelector<HTMLButtonElement>(
    "[data-menu-trigger]",
  );
  const spinner = control.querySelector<HTMLElement>("[data-move-pending]");
  if (trigger !== null) {
    trigger.disabled = isPending;
    trigger.setAttribute("aria-busy", String(isPending));
    // Disabling a focused control drops focus to `<body>`; a keyboard operator
    // would land back at the top of the page when the move settles.
    if (!isPending && document.activeElement === document.body) trigger.focus();
  }
  if (spinner !== null) spinner.hidden = !isPending;
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
    const existing = rowFor(root, task.code);
    if (existing !== null) {
      if (!isFresher(occurredAt, existing.dataset.updatedAt)) continue;
      patchRow(root, existing, task, occurredAt);
      pulse(existing);
      continue;
    }
    const row = cloneTemplate(root, "task");
    if (row === null) continue;
    row.dataset.code = task.code;
    setField(row, "task-code", task.code);
    patchRow(root, row, task, occurredAt);
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
function patchRow(
  root: HTMLElement,
  row: HTMLElement,
  task: TaskItem,
  occurredAt: string,
): void {
  setField(row, "task-title", task.title);
  const link = row.querySelector<HTMLAnchorElement>("[data-task-link]");
  if (link !== null) link.href = `/tasks/${encodeURIComponent(task.code)}`;

  const chip = row.querySelector<HTMLElement>("[data-state-chip]");
  if (chip !== null) {
    applyTone(chip, stateTone(task.state));
    setField(chip, "state-label", task.state.label);
    chip.dataset.category = task.state.category;
  }

  const priorityChip = row.querySelector<HTMLElement>("[data-priority-chip]");
  if (priorityChip !== null) {
    applyTone(priorityChip, taxonomyTone(task.priority.color));
    setField(priorityChip, "priority-label", task.priority.label);
  }

  const dueControl = row.querySelector<HTMLElement>("[data-date-control]");
  if (dueControl !== null) {
    dueControl.dataset.code = task.code;
    renderDateControl(dueControl, task.due_date ?? null);
  }

  const overdue = row.querySelector<HTMLElement>("[data-field='overdue']");
  if (overdue !== null) overdue.hidden = !task.is_overdue;

  const owner = row.querySelector<HTMLElement>("[data-owner-control]");
  if (owner !== null) {
    owner.dataset.code = task.code;
    renderOwnerControl(owner, task.assignee ?? null);
  }

  patchMoves(row, task);
  patchDependencies(root, row, task);
  row.dataset.updatedAt = occurredAt;
}

/**
 * Rewrites a row's legal moves from the fresh read.
 *
 * A move that was legal a second ago may not be now — that is the entire reason
 * the list is refetched rather than kept. With none published the trigger goes
 * dead, which is also how a task that reached a terminal state renders.
 */
function patchMoves(row: HTMLElement, task: TaskItem): void {
  const control = row.querySelector<HTMLElement>("[data-move-control]");
  if (control === null) return;
  control.dataset.code = task.code;

  const panel = control.querySelector<HTMLElement>("[data-menu-panel]");
  const trigger = control.querySelector<HTMLButtonElement>(
    "[data-menu-trigger]",
  );
  if (panel === null || trigger === null) return;

  const moves: readonly TaskMove[] = taskMoves(task);
  panel.replaceChildren();
  for (const move of moves) {
    const option = cloneTemplate(control, "move-option");
    if (option === null) continue;
    option.dataset.toState = move.toStateCode;
    option.dataset.requiresReason = String(move.requiresReason);
    setField(option, "move-label", move.label);
    const meta = option.querySelector<HTMLElement>("[data-field='move-meta']");
    if (meta !== null) meta.hidden = !move.requiresReason;
    panel.appendChild(option);
  }

  // A pending move keeps the trigger disabled regardless: the row must not
  // become clickable again just because a refresh landed mid-flight.
  const isDead = moves.length === 0;
  trigger.disabled = isDead || trigger.getAttribute("aria-busy") === "true";
  trigger.setAttribute(
    "aria-label",
    `Cambiar estado. Ahora: ${task.state.label}`,
  );

  // The explanation is re-keyed per row — a clone carries the template's id —
  // and attached only while the trigger is actually dead.
  const reason = control.querySelector<HTMLElement>("[data-move-reason]");
  if (reason !== null) reason.id = `moves-off-${task.code}`;
  if (isDead) {
    trigger.title = TASK_MOVES_UNAVAILABLE;
    if (reason !== null) trigger.setAttribute("aria-describedby", reason.id);
    return;
  }
  trigger.removeAttribute("title");
  trigger.removeAttribute("aria-describedby");
}

/**
 * Rewrites the dependency chips.
 *
 * `task_code` when the reference resolved, the operation's own words otherwise
 * — dropping an unresolved label would delete the note somebody actually wrote.
 */
function patchDependencies(
  root: HTMLElement,
  row: HTMLElement,
  task: TaskItem,
): void {
  const cell = row.querySelector<HTMLElement>("[data-field='dependencies']");
  if (cell === null) return;
  cell.replaceChildren();

  if (task.dependencies.length === 0) {
    const dash = cloneTemplate(root, "no-dependency");
    if (dash !== null) cell.appendChild(dash);
    return;
  }
  for (const dependency of task.dependencies) {
    const chip = cloneTemplate(root, "dependency");
    if (chip === null) continue;
    chip.title = dependency.raw_label;
    chip.textContent = dependency.task_code ?? dependency.raw_label;
    cell.appendChild(chip);
  }
}

/**
 * Wires the "Crear tarea" modal: opens it, operates its controls, submits it.
 *
 * The options are rendered by the server (`CreateTaskDialog`), so there is
 * nothing to fetch here and no moment where the operator faces an empty picker.
 * What this owns is the three controls that are **not** form fields: the two
 * `MenuSelect`s and the `DateField` report a choice through the DOM, not through
 * a `name`, so their values are read off `data-value` and `FormData` is left to
 * the two real inputs.
 *
 * The date's submit is deliberately local. Every other `DateField` on this
 * screen PATCHes the aggregate it names; here the task does not exist yet, so
 * the write is a repaint and the value waits in the DOM until the POST carries
 * it. That is also why the dialog lives outside `[data-tasks]` — the row-level
 * `mountDateFields` is delegated and would otherwise answer these clicks too.
 */
function mountCreateTask(root: HTMLElement, projectCode: string): () => void {
  const opener = root.querySelector<HTMLButtonElement>(
    "[data-action='open-create-task']",
  );
  const dialog = document.querySelector<HTMLDialogElement>(
    "[data-create-task-dialog]",
  );
  if (opener === null || dialog === null || projectCode === "") return () => {};

  const form = dialog.querySelector<HTMLFormElement>("[data-create-task-form]");
  if (form === null) return () => {};

  const controller = new AbortController();
  const { signal } = controller;

  // What the server chose, so "reset" means "back to the offered default"
  // rather than "back to whatever was picked last time".
  const defaults = new Map<HTMLElement, string>();
  for (const control of menuControls(dialog)) {
    defaults.set(control, control.dataset.value ?? "");
  }

  const releaseMenus = mountMenus(dialog, "[data-menu-select]", {
    // Server-rendered; the panel is already correct when it opens.
    fill: () => Promise.resolve(),
    choose: (control, item) => {
      chooseMenuOption(control, item);
      return Promise.resolve();
    },
  });

  const releaseDates = mountDateFields(dialog, (control, value) => {
    renderDateControl(control, value);
    return Promise.resolve();
  });

  opener.addEventListener(
    "click",
    () => {
      resetCreateForm(dialog, form, defaults);
      dialog.showModal();
      form.querySelector<HTMLInputElement>("[name='title']")?.focus();
    },
    { signal },
  );

  dialog.addEventListener(
    "click",
    (event) => {
      const target = event.target;
      if (!(target instanceof Element)) return;
      if (target.closest("[data-action='close-create']") !== null) {
        dialog.close();
      }
    },
    { signal },
  );

  form.addEventListener(
    "submit",
    (event) => {
      event.preventDefault();
      void submitCreate(root, dialog, form, projectCode);
    },
    { signal },
  );

  return () => {
    controller.abort();
    releaseMenus();
    releaseDates();
    if (dialog.open) dialog.close();
  };
}

/** Every `MenuSelect` of the create dialog, in the order it renders them. */
function menuControls(dialog: HTMLDialogElement): readonly HTMLElement[] {
  return [...dialog.querySelectorAll<HTMLElement>("[data-menu-select]")];
}

/**
 * Paints a `MenuSelect`'s choice, which is also how it stores it.
 *
 * The control has no hidden input: `data-value` on the wrapper *is* the field,
 * and the trigger's label and the ticks are what the operator reads back. All
 * three move together here so a chosen value can never disagree with the word
 * beside it.
 */
function chooseMenuOption(control: HTMLElement, item: HTMLElement): void {
  control.dataset.value = item.dataset.value ?? "";
  setField(control, "menu-current", (item.textContent ?? "").trim());

  for (const option of control.querySelectorAll<HTMLElement>(
    "[data-menu-item]",
  )) {
    const isChosen = option === item;
    option.setAttribute("aria-checked", String(isChosen));
    const tick = option.querySelector<HTMLElement>("[data-option-tick]");
    if (tick !== null) tick.hidden = !isChosen;
  }
}

/** The value a `MenuSelect` currently holds, `""` when it holds none. */
function menuValue(dialog: HTMLDialogElement, facet: string): string {
  const control = dialog.querySelector<HTMLElement>(
    `[data-menu-select][data-facet="${CSS.escape(facet)}"]`,
  );
  return control?.dataset.value ?? "";
}

/** The date the dialog's single `DateField` holds, `""` when it holds none. */
function createDueDate(dialog: HTMLDialogElement): string {
  const control = dialog.querySelector<HTMLElement>("[data-date-control]");
  return control?.dataset.value ?? "";
}

/**
 * Returns the whole dialog to the state the server rendered.
 *
 * Called on every open rather than on close: a submission that failed keeps its
 * words on screen, and the operator who reopens the dialog starts clean.
 */
function resetCreateForm(
  dialog: HTMLDialogElement,
  form: HTMLFormElement,
  defaults: ReadonlyMap<HTMLElement, string>,
): void {
  form.reset();
  showCreateError(dialog, null);

  for (const [control, value] of defaults) {
    const item = control.querySelector<HTMLElement>(
      `[data-menu-item][data-value="${CSS.escape(value)}"]`,
    );
    if (item !== null) chooseMenuOption(control, item);
  }

  const date = dialog.querySelector<HTMLElement>("[data-date-control]");
  if (date !== null) renderDateControl(date, null);
}

/** Reads the dialog, posts it, and lets the refresh paint the new row. */
async function submitCreate(
  root: HTMLElement,
  dialog: HTMLDialogElement,
  form: HTMLFormElement,
  projectCode: string,
): Promise<void> {
  const data = new FormData(form);
  const title = String(data.get("title") ?? "").trim();
  const dependsOn = String(data.get("depends_on") ?? "").trim();
  const priority = menuValue(dialog, "create-priority");
  const assignee = menuValue(dialog, "create-assignee");
  const dueDate = createDueDate(dialog);

  // Checked here as well as by `required`, because a title of only spaces
  // satisfies the browser and would create a task nobody can identify. No
  // request leaves until both are answered.
  if (title === "" || priority === "") {
    showCreateError(dialog, CREATE_INCOMPLETE);
    return;
  }
  showCreateError(dialog, null);

  const button = dialog.querySelector<HTMLButtonElement>(
    "[data-action='create-task']",
  );
  if (button !== null) setPending(button, true);

  const result = await postProjectTask(projectCode, {
    title,
    priority,
    detail: "",
    description: "",
    last_progress: "",
    due_date: dueDate === "" ? null : dueDate,
    ...(assignee === "" ? {} : { assignee }),
    ...(dependsOn === "" ? {} : { depends_on: [dependsOn] }),
  });

  if (button !== null) setPending(button, false);

  if (!result.ok) {
    // The dialog stays open on every failure: closing it would take the
    // operator's five answers with it.
    if (result.error.kind === "validation") {
      showCreateError(dialog, joinFields(Object.keys(result.error.fields)));
      return;
    }
    const copy = failureCopy(result.error);
    toast({ kind: "error", title: copy.title, detail: copy.detail });
    return;
  }

  dialog.close();
  // Refreshed rather than cloned from the response: `refresh` is the one place
  // that also moves the tab count and flips the empty arm, and the
  // `task.created` envelope that follows is a no-op under `isFresher`.
  await refresh(root, projectCode, new Date().toISOString());
  toast({
    kind: "success",
    title: "Tarea creada",
    detail: `${result.data.code} quedó en ${result.data.state.label}.`,
  });
}

/** Shows or clears the dialog's inline failure line. */
function showCreateError(
  dialog: HTMLDialogElement,
  message: string | null,
): void {
  const line = dialog.querySelector<HTMLElement>("[data-field='create-error']");
  if (line === null) return;
  line.hidden = message === null;
  line.textContent = message ?? "";
}
