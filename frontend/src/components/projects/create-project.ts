/**
 * The live half of `CreateProjectDialog.astro`: the controls that are not form
 * fields, the guards that run before any request, and the one POST.
 *
 * **Nothing is fetched here.** Every option in the sheet was rendered by
 * `/projects` in its frontmatter, so this module has no loading arm and no
 * moment where the operator faces an empty picker — the same contract
 * `create-task.ts` keeps. What it owns is the four `MenuSelect`s and the two
 * `DateField`s, which report their answers through the DOM rather than through a
 * `name`, so they are read off the control and `FormData` is left to the real
 * inputs.
 *
 * **The date writes are local.** Every other `DateField` in the app PATCHes the
 * aggregate it names; here the project does not exist yet, so the write is a
 * repaint and the value waits in the DOM until the POST carries it. That is also
 * why the dialog is rendered outside `[data-projects]`: a delegated row writer
 * would otherwise answer these clicks and PATCH a project that has no code.
 *
 * **The opening protocol is one document-level event.** Any surface that wants
 * to offer creation dispatches {@link CREATE_PROJECT_EVENT} or renders a
 * `[data-action='open-create-project']` button; neither has to import this
 * module, and this module does not have to know they exist.
 *
 * **Nothing is inserted optimistically.** A project renders twice on `/projects`
 * — as a card and as a row — and the filter island dedupes by `data-code`, so a
 * hand-built insert would mean re-implementing `presentation.ts` in the browser.
 * The surface is repainted by navigating to itself through the client router,
 * which keeps the SSE connection and every other island alive; `location.reload`
 * would tear both down.
 */

import { navigate } from "astro:transitions/client";

import { postProject } from "../../lib/api/client";
import type { ProjectCreateIn } from "../../lib/api/domain";
import { toast } from "../../lib/toast";
import { mountMenus } from "../../lib/ui/menu";
import { mountDateFields, renderDateControl } from "../ui/date-field";
import { setPending } from "./dom";
import {
  CREATE_PROJECT_CODE_RACE,
  CREATE_PROJECT_INCOMPLETE,
  CREATE_PROJECT_VALUE_INVALID,
  createProjectDuplicateWarning,
} from "./create-project-copy";
import { failureCopy, joinFields } from "./messages";

/**
 * The document event that opens the sheet.
 *
 * A `CustomEvent` on `document` rather than an exported `open()`, so no surface
 * has to import another to offer creation: the page header, the empty state and
 * anything added later all say the same sentence into the room, and exactly one
 * listener answers it.
 */
export const CREATE_PROJECT_EVENT = "aztec:create-project";

/** The facets the sheet's pickers are addressed by; the markup's side of the pact. */
const FACET = {
  client: "create-project-client",
  engagement: "create-project-engagement",
  owner: "create-project-owner",
  currency: "create-project-currency",
  projectType: "create-project-type",
  stage: "create-project-stage",
} as const;

/**
 * Mounts the create-project sheet.
 *
 * @param root - The `<dialog>` carrying `data-create-project-dialog`.
 * @returns The teardown; drops every listener and closes the sheet. A page
 *   without the dialog never reaches this, because `onPage` finds no root.
 */
export function mountCreateProject(root: HTMLElement): () => void {
  if (!(root instanceof HTMLDialogElement)) return () => {};
  const dialog = root;

  const form = dialog.querySelector<HTMLFormElement>(
    "[data-create-project-form]",
  );
  if (form === null) return () => {};

  const controller = new AbortController();
  const { signal } = controller;

  // What the server chose, so "reset" means "back to the offered default"
  // rather than "back to whatever was picked last time".
  const defaults = new Map<HTMLElement, string>();
  for (const control of dialog.querySelectorAll<HTMLElement>(
    "[data-menu-select]",
  )) {
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

  const releaseDates = mountDateFields(dialog, (control, value) => {
    renderDateControl(control, value);
    return Promise.resolve();
  });

  /**
   * The name+client pair the operator already answered "Crear igualmente" for.
   *
   * Keyed rather than a boolean: acknowledging one duplicate must not silence
   * the warning for a different name typed afterwards in the same sheet.
   */
  let acknowledged: string | null = null;

  const open = (): void => {
    if (dialog.open) return;
    acknowledged = null;
    resetForm(dialog, form, defaults);
    dialog.showModal();
    form.querySelector<HTMLInputElement>("[name='name']")?.focus();
  };

  document.addEventListener(CREATE_PROJECT_EVENT, open, { signal });

  document.addEventListener(
    "click",
    (event) => {
      const target = event.target;
      if (!(target instanceof Element)) return;
      if (target.closest("[data-action='open-create-project']") === null)
        return;
      open();
    },
    { signal },
  );

  dialog.addEventListener(
    "click",
    (event) => {
      const target = event.target;
      if (!(target instanceof Element)) return;

      if (target.closest("[data-action='close-create-project']") !== null) {
        dialog.close();
        return;
      }
      if (target.closest("[data-action='review-duplicate']") !== null) {
        showDuplicate(dialog, null);
        form.querySelector<HTMLInputElement>("[name='name']")?.focus();
        return;
      }
      if (target.closest("[data-action='create-anyway']") !== null) {
        acknowledged = duplicateKey(dialog, form);
        showDuplicate(dialog, null);
        void submit(dialog, form, acknowledged);
      }
    },
    { signal },
  );

  form.addEventListener(
    "submit",
    (event) => {
      event.preventDefault();
      void submit(dialog, form, acknowledged);
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

/**
 * Paints a `MenuSelect`'s choice, which is also how it stores it.
 *
 * The control has no hidden input: `data-value` on the wrapper *is* the field,
 * and the trigger's label and the ticks are what the operator reads back. All
 * three move together here, so a chosen value can never disagree with the word
 * beside it.
 */
function chooseMenuOption(control: HTMLElement, item: HTMLElement): void {
  control.dataset["value"] = item.dataset["value"] ?? "";
  const current = control.querySelector<HTMLElement>(
    "[data-field='menu-current']",
  );
  if (current !== null) current.textContent = (item.textContent ?? "").trim();

  for (const option of control.querySelectorAll<HTMLElement>(
    "[data-menu-item]",
  )) {
    const isChosen = option === item;
    option.setAttribute("aria-checked", String(isChosen));
    const tick = option.querySelector<HTMLElement>("[data-option-tick]");
    if (tick !== null) tick.hidden = !isChosen;
  }
}

/** The `MenuSelect` for one facet of this sheet, or `null` when it is absent. */
function menuControl(
  dialog: HTMLDialogElement,
  facet: string,
): HTMLElement | null {
  return dialog.querySelector<HTMLElement>(
    `[data-menu-select][data-facet="${CSS.escape(facet)}"]`,
  );
}

/** The value a `MenuSelect` currently holds, `""` when it holds none. */
function menuValue(dialog: HTMLDialogElement, facet: string): string {
  return menuControl(dialog, facet)?.dataset["value"] ?? "";
}

/** The words the operator is reading for that choice, for the copy that quotes it. */
function menuLabel(dialog: HTMLDialogElement, facet: string): string {
  const control = menuControl(dialog, facet);
  return (
    control?.querySelector<HTMLElement>("[data-field='menu-current']")
      ?.textContent ?? ""
  ).trim();
}

/**
 * Whether the owner picker offers anybody at all.
 *
 * "Sin responsable" is always there, so a lone entry means the roster read
 * failed. The submit guard requires an owner exactly when there is somebody to
 * pick: demanding one from a picker that cannot offer one would turn a failed
 * second read into a refusal to register the project (`create-project-copy.ts`).
 */
function hasRoster(dialog: HTMLDialogElement): boolean {
  const control = menuControl(dialog, FACET.owner);
  if (control === null) return false;
  return [...control.querySelectorAll<HTMLElement>("[data-menu-item]")].some(
    (item) => (item.dataset["value"] ?? "") !== "",
  );
}

/** The date one of the sheet's two `DateField`s holds, `""` when it holds none. */
function dateValue(dialog: HTMLDialogElement, field: string): string {
  const control = dialog.querySelector<HTMLElement>(
    `[data-field-date="${CSS.escape(field)}"] [data-date-control]`,
  );
  return control?.dataset["value"] ?? "";
}

/** One text field of the form, trimmed; `""` when it was left alone. */
function textValue(form: HTMLFormElement, name: string): string {
  return String(new FormData(form).get(name) ?? "").trim();
}

/**
 * The typed amount as a number, or the fact that it is not one.
 *
 * `"empty"` and `"invalid"` are different answers: leaving the box alone is a
 * legitimate project with one signal fewer, while typing "mucho" is a mistake
 * the operator has to see before a request leaves.
 */
type ParsedValue =
  { kind: "empty" } | { kind: "amount"; value: number } | { kind: "invalid" };

function parseBusinessValue(raw: string): ParsedValue {
  if (raw === "") return { kind: "empty" };
  // A Spanish keyboard writes the decimal separator as a comma, and rejecting
  // that would be rejecting the operator's own locale.
  const amount = Number(raw.replace(",", "."));
  if (!Number.isFinite(amount) || amount < 0) return { kind: "invalid" };
  return { kind: "amount", value: amount };
}

/** The name+client pair a duplicate acknowledgement is remembered by. */
function duplicateKey(
  dialog: HTMLDialogElement,
  form: HTMLFormElement,
): string {
  return `${textValue(form, "name").toLocaleLowerCase("es")}|${menuValue(dialog, FACET.client)}`;
}

/**
 * Whether the portfolio already shows this name for this client.
 *
 * Read from the rendered rows rather than from a request: the cards and the
 * table *are* the portfolio this operator is looking at, so the warning cannot
 * disagree with what is on screen, and a page whose queue read failed simply
 * warns about nothing — which is the harmless direction for a check that never
 * blocks anyway.
 */
function isDuplicate(name: string, clientLabel: string): boolean {
  if (name === "" || clientLabel === "") return false;
  const needle = name.toLocaleLowerCase("es");
  const client = clientLabel.toLocaleLowerCase("es");
  return [...document.querySelectorAll<HTMLElement>("[data-project]")].some(
    (node) =>
      (node.dataset["sortName"] ?? "") === needle &&
      (node.dataset["search"] ?? "").includes(client),
  );
}

/**
 * Returns the sheet to the state it was opened with.
 *
 * On open rather than on close: a submission that failed keeps its words on
 * screen, and the operator who reopens the sheet starts clean.
 */
function resetForm(
  dialog: HTMLDialogElement,
  form: HTMLFormElement,
  defaults: ReadonlyMap<HTMLElement, string>,
): void {
  form.reset();
  showError(dialog, null);
  showDuplicate(dialog, null);

  for (const [control, value] of defaults) {
    const item = control.querySelector<HTMLElement>(
      `[data-menu-item][data-value="${CSS.escape(value)}"]`,
    );
    if (item !== null) chooseMenuOption(control, item);
  }

  for (const control of dialog.querySelectorAll<HTMLElement>(
    "[data-date-control]",
  )) {
    renderDateControl(control, null);
  }

  // `form.reset()` does not close a `<details>`, and an operator reopening the
  // sheet should meet the six fields the ordering exists for, not twelve.
  const more = dialog.querySelector("details");
  if (more !== null) more.open = false;
}

/**
 * Reads the sheet, guards it, posts it, and repaints `/projects`.
 *
 * Every guard runs before any request: `required` accepts a name of only
 * spaces, and a non-numeric amount would come back as a 422 the operator has to
 * decode. The sheet stays open on every failure — closing it would take a dozen
 * answers with it.
 */
async function submit(
  dialog: HTMLDialogElement,
  form: HTMLFormElement,
  acknowledged: string | null,
): Promise<void> {
  const name = textValue(form, "name");
  const client = menuValue(dialog, FACET.client);
  const engagement = menuValue(dialog, FACET.engagement);
  const owner = menuValue(dialog, FACET.owner);

  if (
    name === "" ||
    client === "" ||
    engagement === "" ||
    (owner === "" && hasRoster(dialog))
  ) {
    showDuplicate(dialog, null);
    showError(dialog, CREATE_PROJECT_INCOMPLETE);
    return;
  }

  const amount = parseBusinessValue(textValue(form, "business_value"));
  if (amount.kind === "invalid") {
    showDuplicate(dialog, null);
    showError(dialog, CREATE_PROJECT_VALUE_INVALID);
    return;
  }
  showError(dialog, null);

  if (
    acknowledged !== duplicateKey(dialog, form) &&
    isDuplicate(name, menuLabel(dialog, FACET.client))
  ) {
    showDuplicate(dialog, createProjectDuplicateWarning(name));
    return;
  }
  showDuplicate(dialog, null);

  const projectType = menuValue(dialog, FACET.projectType);
  const stage = menuValue(dialog, FACET.stage);
  const startDate = dateValue(dialog, "start");
  const targetDate = dateValue(dialog, "target");

  // Built key by key, never by spreading the form: an omitted key and an
  // explicit `null` are different instructions on this API, and a spread turns
  // "no lo sé todavía" into whatever the input happened to hold.
  const body: ProjectCreateIn = {
    name,
    client,
    engagement_type: engagement,
    currency: menuValue(dialog, FACET.currency),
    summary: textValue(form, "summary"),
    description: textValue(form, "description"),
    next_step: textValue(form, "next_step"),
    ...(owner === "" ? {} : { owner }),
    ...(projectType === "" ? {} : { project_type: projectType }),
    ...(stage === "" ? {} : { stage }),
    ...(startDate === "" ? {} : { start_date: startDate }),
    ...(targetDate === "" ? {} : { target_date: targetDate }),
    ...(amount.kind === "amount" ? { business_value: amount.value } : {}),
  };

  const button = dialog.querySelector<HTMLButtonElement>(
    "[data-action='create-project']",
  );
  if (button !== null) setPending(button, true);

  // The code is allocated inside the creating transaction, so a 409 here means
  // a simultaneous creation took the number and *nothing was written*. Retrying
  // once, silently, is therefore safe and is what the operator would do anyway;
  // a second one is a real collision and gets said out loud.
  let result = await postProject(body);
  if (!result.ok && result.error.code === "conflicting_state") {
    result = await postProject(body);
  }

  if (button !== null) setPending(button, false);

  if (!result.ok) {
    if (result.error.code === "conflicting_state") {
      showError(dialog, CREATE_PROJECT_CODE_RACE);
      return;
    }
    if (result.error.kind === "validation") {
      showError(dialog, joinFields(Object.keys(result.error.fields)));
      return;
    }
    const copy = failureCopy(result.error);
    toast({ kind: "error", title: copy.title, detail: copy.detail });
    return;
  }

  const created = result.data;
  dialog.close();

  // Announced before the navigation, not after: the toast region lives on the
  // shell, which survives a client-router swap, while anything queued after the
  // await would land on a page that has already replaced this script's DOM.
  toast({
    kind: "success",
    title: "Proyecto creado",
    detail: `${created.code} quedó en ${created.state.label}.`,
  });
  await navigate(window.location.href);
}

/** Shows or clears the sheet's inline failure line. */
function showError(dialog: HTMLDialogElement, message: string | null): void {
  const line = dialog.querySelector<HTMLElement>(
    "[data-field='create-project-error']",
  );
  if (line === null) return;
  line.hidden = message === null;
  line.textContent = message ?? "";
}

/** Shows or clears the duplicate warning and its two answers. */
function showDuplicate(
  dialog: HTMLDialogElement,
  message: string | null,
): void {
  const block = dialog.querySelector<HTMLElement>(
    "[data-field='create-project-duplicate']",
  );
  if (block === null) return;
  block.hidden = message === null;
  const text = block.querySelector<HTMLElement>(
    "[data-field='create-project-duplicate-text']",
  );
  if (text !== null) text.textContent = message ?? "";
}
