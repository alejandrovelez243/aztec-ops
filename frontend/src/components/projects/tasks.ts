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
 * **Removing.** Rows are patched in place, new ones are appended, and — since
 * `DELETE /api/v1/tasks/{code}` exists — strays are reconciled away against the
 * fresh read. That last part is guarded on the read being *complete*
 * (`items.length === count`): this table renders one page, and reconciling a
 * partial one would delete every row that merely fell off it. When the guard
 * does not hold the table keeps its extra rows, which is the harmless failure.
 */

import { navigate } from "astro:transitions/client";

import { getProjectTasks, postTaskTransition } from "../../lib/api/client";
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
} from "./dom";
import { closeMenu, mountMenus } from "../../lib/ui/menu";
import { failureCopy, TASK_MOVES_UNAVAILABLE } from "./messages";
import { mountCreateTask } from "./create-task";
import type { MultiOption } from "../ui/multi-select";
import { mountOwnerMenus, renderOwnerControl } from "./owner-menu";
import { mountPriorityMenus, renderPriorityControl } from "./priority-menu";
import { askReason } from "./reason-dialog";
import { markEvent } from "./stale";
import { taskMoves, type TaskMove } from "./task-moves";
import {
  submitTaskDueDate,
  submitTaskOwner,
  submitTaskPriority,
  submitTaskRemoval,
} from "../tasks/task-writes";
import { mountDateFields, renderDateControl } from "../ui/date-field";
import { stateTone } from "./tone";
import type { TaskItem } from "../../lib/api/domain";

/** Topics that can change something a task row shows. */
const WATCHED: readonly Topic[] = [
  "task.created",
  "task.updated",
  "task.state_changed",
  // One topic for both directions, carrying `is_archived`. A colleague's
  // removal — and their undo — therefore reaches this table live, which matters
  // more here than for any other change: a row nobody can act on any more is
  // worse than a stale label.
  "task.archive_changed",
];

/** How long a submitted move waits for the stream before trusting the POST. */
const STREAM_CONFIRM_MS = 8_000;

/**
 * Rows per re-read, sent explicitly rather than left to the server's default.
 *
 * It mirrors what the page's first paint asked for, so the island reads the same
 * page it is patching. Sending it by hand is what keeps the reconciliation guard
 * honest: a silent default is a ceiling nobody notices moving.
 */
const TASKS_PAGE_SIZE = 50;

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

  /**
   * Drops a task's pending move without settling it.
   *
   * Called when its row leaves the table: the timer would otherwise fire against
   * a detached control, and `settle` would toast about a task that is gone.
   */
  const forget = (taskCode: string): void => {
    const move = pending.get(taskCode);
    if (move === undefined) return;
    pending.delete(taskCode);
    window.clearTimeout(move.timer);
  };

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
      void refresh(root, code, envelope.occurred_at, forget).finally(() => {
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
          void refresh(root, code, new Date().toISOString(), forget);
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

  /**
   * The row's overflow: open the task, or remove it.
   *
   * Both items are `<button>`s because that is what `menu.ts` walks, so "Abrir"
   * navigates here rather than being an `<a>`. `navigate()` from the client
   * router, never `window.location`: a full reload would tear down the stream
   * connection and every island on the page to reach a route the router already
   * serves.
   */
  const offRowMenus = mountMenus(root, "[data-row-menu]", {
    fill: () => Promise.resolve(),
    choose: async (control, item) => {
      const taskCode = control.dataset["code"] ?? "";
      if (taskCode === "") return;

      if (item.dataset["rowAction"] === "open") {
        await navigate(taskHref(taskCode));
        return;
      }
      if (item.dataset["rowAction"] !== "remove") return;

      await submitTaskRemoval(taskCode, async () => {
        await refresh(root, code, new Date().toISOString(), forget);
      });
    },
  });

  const offOwners = mountOwnerMenus(root, submitTaskOwner);
  const offPriorities = mountPriorityMenus(root, submitTaskPriority);
  const offDates = mountDateFields(root, submitTaskDueDate);

  /**
   * The create dialog, shared with the board (`create-task.ts`).
   *
   * The table knows its project from the markup and its dependency options from
   * the rows it has on screen — including the ones the stream appended after the
   * first paint, which is why they are read on every open rather than captured
   * here.
   */
  const opener = root.querySelector<HTMLButtonElement>(
    "[data-action='open-create-task']",
  );
  const offCreate =
    opener === null || code === ""
      ? () => {}
      : mountCreateTask({
          opener,
          projectCode: () => code,
          dependencies: () => dependencyOptions(root),
          onCreated: async () => {
            // Refreshed rather than cloned from the response: `refresh` is the
            // one place that also moves the tab count and flips the empty arm,
            // and the `task.created` envelope that follows is a no-op under
            // `isFresher`.
            await refresh(root, code, new Date().toISOString(), forget);
          },
        });

  return () => {
    for (const off of offs) off();
    offMoves();
    offRowMenus();
    offOwners();
    offPriorities();
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

/**
 * Where a task's own screen lives.
 *
 * Duplicated from `TasksTable.astro`'s frontmatter deliberately: that copy runs
 * on the server and this one in the browser, and the two builds cannot share a
 * value without one of them importing the other's module.
 */
function taskHref(taskCode: string): string {
  return `/tasks/${encodeURIComponent(taskCode)}`;
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

/**
 * Re-reads the tasks, patches the rows that changed and drops the ones that are
 * gone.
 *
 * @param root - The element carrying `data-tasks`.
 * @param code - The project whose tasks are being read.
 * @param occurredAt - The freshness floor for the patch; a row already stamped
 *   at or after this is left alone.
 * @param forget - Releases a removed task's pending move, so its timer cannot
 *   fire against a detached control.
 */
async function refresh(
  root: HTMLElement,
  code: string,
  occurredAt: string,
  forget: (taskCode: string) => void,
): Promise<void> {
  const result = await getProjectTasks(code, { page_size: TASKS_PAGE_SIZE });
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

  // Only a complete read can say a row is gone. On a partial page the rows
  // beyond it are absent because of paging, not because anybody removed them.
  if (result.data.items.length === count) {
    const live = new Set(result.data.items.map((task) => task.code));
    for (const row of [
      ...body.querySelectorAll<HTMLElement>("[data-task-row]"),
    ]) {
      const rowCode = row.dataset["code"] ?? "";
      if (rowCode === "" || live.has(rowCode)) continue;
      forget(rowCode);
      removeRow(row);
    }
  }

  const tabs = document.querySelector<HTMLElement>("[data-tabs]");
  if (tabs !== null) setField(tabs, "tasks-count", String(count));

  const empty = root.querySelector<HTMLElement>("[data-tasks-empty]");
  const wrap = root.querySelector<HTMLElement>("[data-tasks-wrap]");
  if (empty !== null) empty.hidden = count > 0;
  if (wrap !== null) wrap.hidden = count === 0;
}

/**
 * Takes one row off the table without leaving anything of it behind.
 *
 * Three things outlive a naive `remove()`, and each is a real defect:
 *
 * - `menu.ts` holds a module-global reference to the open panel. Removing its
 *   host leaves a `position: fixed` panel floating beside nothing, still
 *   answering clicks for a task that is gone — so the menu is closed first,
 *   unconditionally: closing an already-closed menu is free.
 * - Focus. Removing the element that holds `document.activeElement` drops focus
 *   to `<body>`, which sends a keyboard operator back to the top of the page
 *   mid-gesture. It is handed to the next row's kebab instead — the previous
 *   row's when the last one went — so the operator stays where they were.
 * - The row's own controls are delegated, so nothing else needs unmounting.
 */
function removeRow(row: HTMLElement): void {
  closeMenu(false);

  const active = document.activeElement;
  const heldFocus = active instanceof Node && row.contains(active);
  const successor =
    row.nextElementSibling instanceof HTMLElement
      ? row.nextElementSibling
      : row.previousElementSibling instanceof HTMLElement
        ? row.previousElementSibling
        : null;

  row.remove();

  if (!heldFocus) return;
  successor
    ?.querySelector<HTMLButtonElement>("[data-row-menu] [data-menu-trigger]")
    ?.focus();
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
  if (link !== null) link.href = taskHref(task.code);

  // A cloned row carries the template's empty code; the overflow has to learn
  // which task it acts on before it can be opened, let alone used to remove one.
  const rowMenu = row.querySelector<HTMLElement>("[data-row-menu]");
  if (rowMenu !== null) {
    rowMenu.dataset["code"] = task.code;
    rowMenu
      .querySelector<HTMLButtonElement>("[data-menu-trigger]")
      ?.setAttribute("aria-label", `Acciones de ${task.title}`);
  }

  const chip = row.querySelector<HTMLElement>("[data-state-chip]");
  if (chip !== null) {
    applyTone(chip, stateTone(task.state));
    setField(chip, "state-label", task.state.label);
    chip.dataset.category = task.state.category;
  }

  // The chip is a picker now, so it is repainted through the control's own
  // painter: a priority this row set and one a colleague set must leave the
  // trigger *and* the ticked option reading the same thing.
  const priority = row.querySelector<HTMLElement>("[data-priority-control]");
  if (priority !== null) {
    priority.dataset["code"] = task.code;
    renderPriorityControl(priority, task.priority);
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
 * The project's tasks as dependency options for the create dialog.
 *
 * Read from the rendered rows rather than from a prop: the rows are the same
 * list the server sent, plus whatever the stream appended since, so a task
 * created a minute ago can already be depended on. `<template>` content is a
 * separate fragment, so the empty row template below is not offered as a task.
 */
function dependencyOptions(root: HTMLElement): readonly MultiOption[] {
  return [...root.querySelectorAll<HTMLElement>("[data-task-row]")].flatMap(
    (row) => {
      const value = row.dataset["code"] ?? "";
      const label = (
        row.querySelector<HTMLElement>("[data-field='task-title']")
          ?.textContent ?? ""
      ).trim();
      return value === "" ? [] : [{ value, label }];
    },
  );
}
