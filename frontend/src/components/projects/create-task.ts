/**
 * The "Crear tarea" modal: one implementation, mounted by every surface that
 * offers creation.
 *
 * The project's task table and the board both create tasks, and they used to do
 * it differently — the table opened this dialog, the board navigated away to the
 * table. Two entry points into one act is how two sets of fields, of copy and of
 * failure handling appear, so the act lives here and the surface supplies only
 * what it alone knows: which project, which tasks may be depended on, and what
 * to repaint afterwards ({@link CreateTaskHost}).
 *
 * The dialog itself is `CreateTaskDialog.astro`, rendered once per page by the
 * page — nothing is fetched here to draw it, so there is no loading arm and no
 * moment where the operator faces an empty picker.
 *
 * What this owns is the four controls that are **not** form fields: the two
 * `MenuSelect`s, the `MultiSelect` and the `DateField` report their answers
 * through the DOM rather than through a `name`, so they are read off the control
 * and `FormData` is left to the single real input.
 *
 * The date's submit is deliberately local. Every other `DateField` in the app
 * PATCHes the aggregate it names; here the task does not exist yet, so the write
 * is a repaint and the value waits in the DOM until the POST carries it. That is
 * also why the dialog is rendered outside any island root that mounts
 * `mountDateFields` for its rows — a delegated row writer would otherwise answer
 * these clicks too and PATCH a task that does not exist.
 */

import { postProjectTask } from "../../lib/api/client";
import { toast } from "../../lib/toast";
import type { TaskItem } from "../../lib/api/domain";
import { setField, setPending } from "./dom";
import { CREATE_INCOMPLETE, failureCopy, joinFields } from "./messages";
import { mountMenus } from "../../lib/ui/menu";
import { mountDateFields, renderDateControl } from "../ui/date-field";
import {
  findMultiSelect,
  mountMultiSelects,
  multiValues,
  setMultiOptions,
} from "../ui/multi-select";
import type { MultiOption } from "../ui/multi-select";

/** The facet of the dialog's dependency picker; the markup's side of the pact. */
const DEPENDS_ON_FACET = "create-depends-on";

/** What a surface must answer for the shared dialog to serve it. */
export interface CreateTaskHost {
  /** The button that opens the dialog on this surface. */
  readonly opener: HTMLButtonElement;
  /**
   * The project the task will hang on, read at the moment of opening.
   *
   * A function rather than a value: the board learns its project from a runtime
   * selection, and a code captured at mount would create tasks on whichever
   * project happened to be selected when the page loaded. `""` refuses to open.
   */
  projectCode(): string;
  /**
   * The tasks offerable as dependencies, read at the moment of opening.
   *
   * From what the surface already has on screen, never from a new request: the
   * rendered rows or cards *are* the project's tasks, so this cannot disagree
   * with what the operator is looking at, and a task created a minute ago is
   * offerable without a reload.
   */
  dependencies(): readonly MultiOption[];
  /**
   * Repaints the surface after the server accepted. Awaited before the success
   * toast, so the new row is on screen by the time it is announced.
   */
  onCreated(projectCode: string, task: TaskItem): Promise<void> | void;
}

/**
 * Wires the create-task dialog for one surface.
 *
 * @param host - What this surface knows and what it wants done afterwards.
 * @returns The teardown; drops every listener and closes the dialog. A page with
 *   no dialog rendered gets a no-op, because an island script runs on every
 *   navigation.
 */
export function mountCreateTask(host: CreateTaskHost): () => void {
  const dialog = document.querySelector<HTMLDialogElement>(
    "[data-create-task-dialog]",
  );
  if (dialog === null) return () => {};

  const form = dialog.querySelector<HTMLFormElement>("[data-create-task-form]");
  if (form === null) return () => {};

  const controller = new AbortController();
  const { signal } = controller;

  // What the server chose, so "reset" means "back to the offered default"
  // rather than "back to whatever was picked last time".
  const defaults = new Map<HTMLElement, string>();
  for (const control of menuControls(dialog)) {
    defaults.set(control, control.dataset["value"] ?? "");
  }

  const releaseMenus = mountMenus(dialog, "[data-menu-select]", {
    // Server-rendered; the panel is already correct when it opens.
    fill: () => Promise.resolve(),
    choose: (control, item) => {
      chooseMenuOption(control, item);
      return Promise.resolve();
    },
  });

  const releaseMulti = mountMultiSelects(dialog);

  const releaseDates = mountDateFields(dialog, (control, value) => {
    renderDateControl(control, value);
    return Promise.resolve();
  });

  host.opener.addEventListener(
    "click",
    () => {
      const projectCode = host.projectCode();
      // The opener is disabled without a project, so this is the belt to that
      // brace: a dialog opened with no project would post to `/projects//tasks`.
      if (projectCode === "") return;
      dialog.dataset["projectCode"] = projectCode;
      setField(dialog, "create-project", projectCode);
      const depends = findMultiSelect(dialog, DEPENDS_ON_FACET);
      if (depends !== null) setMultiOptions(depends, host.dependencies());
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
      void submitCreate(dialog, form, host);
    },
    { signal },
  );

  return () => {
    controller.abort();
    releaseMenus();
    releaseMulti();
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
  control.dataset["value"] = item.dataset["value"] ?? "";
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
  return control?.dataset["value"] ?? "";
}

/** The date the dialog's single `DateField` holds, `""` when it holds none. */
function createDueDate(dialog: HTMLDialogElement): string {
  const control = dialog.querySelector<HTMLElement>("[data-date-control]");
  return control?.dataset["value"] ?? "";
}

/** The dependency codes the operator chose, in the order they chose them. */
function createDependencies(dialog: HTMLDialogElement): readonly string[] {
  const control = findMultiSelect(dialog, DEPENDS_ON_FACET);
  return control === null ? [] : multiValues(control);
}

/**
 * Returns the dialog to the state it was opened with.
 *
 * Called on every open rather than on close: a submission that failed keeps its
 * words on screen, and the operator who reopens the dialog starts clean. The
 * dependency chips are already cleared by `setMultiOptions`, which runs first
 * precisely because a chip from the previous project must not survive.
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

/** Reads the dialog, posts it, and lets the surface paint the new task. */
async function submitCreate(
  dialog: HTMLDialogElement,
  form: HTMLFormElement,
  host: CreateTaskHost,
): Promise<void> {
  const projectCode = dialog.dataset["projectCode"] ?? "";
  const data = new FormData(form);
  const title = String(data.get("title") ?? "").trim();
  const priority = menuValue(dialog, "create-priority");
  const assignee = menuValue(dialog, "create-assignee");
  const dueDate = createDueDate(dialog);
  const dependsOn = createDependencies(dialog);

  // Checked here as well as by `required`, because a title of only spaces
  // satisfies the browser and would create a task nobody can identify. No
  // request leaves until both are answered.
  if (projectCode === "" || title === "" || priority === "") {
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
    // Omitted entirely rather than sent empty: choosing no dependency is a
    // legitimate answer, and `[]` would be a claim the operator never made.
    ...(dependsOn.length === 0 ? {} : { depends_on: [...dependsOn] }),
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
  await host.onCreated(projectCode, result.data);
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
