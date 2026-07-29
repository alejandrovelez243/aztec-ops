/**
 * The live half of `CreateProjectDialog.astro`: the controls that are not form
 * fields, the guards that run before any request, and the one POST.
 *
 * **Nothing is fetched here.** Every option in the sheet was rendered by the
 * page that hosts it — `/projects` or `/board` — in its frontmatter, so this
 * module has no loading arm and no moment where the operator faces an empty
 * picker — the same contract
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
 * module, and this module does not have to know they exist. The event carries a
 * {@link CreateProjectPlacement} when the surface has one — a band of the
 * board's rail does — and creation then becomes two acts: the POST, and one
 * transition onto the state the operator pressed.
 *
 * **The move is re-checked against the record, never against the graph.** The
 * band was drawn from `GET /api/v1/workflows`, which is configuration; whether
 * *this* project may take that edge is answered only by its own `transitions`
 * (`docs/API.md` §2.2). A move the created project does not list is not posted
 * at all, and every outcome — moved, not moved, unknown — is announced, because
 * the project exists in all three.
 *
 * **Nothing is inserted optimistically.** A project renders twice on `/projects`
 * — as a card and as a row — and the filter island dedupes by `data-code`, so a
 * hand-built insert would mean re-implementing `presentation.ts` in the browser.
 * The surface is repainted by navigating through the client router, which keeps
 * the SSE connection and every other island alive; `location.reload` would tear
 * both down. From the board that navigation is `/board?project=CODE`, so the
 * card appears in whichever band the *server* put it in rather than the one this
 * module hoped for.
 */

import { navigate } from "astro:transitions/client";

import { postProject, postProjectTransition } from "../../lib/api/client";
import type { ProjectCreateIn, ProjectDetail } from "../../lib/api/domain";
import { toast } from "../../lib/toast";
import { mountMenus } from "../../lib/ui/menu";
import type { ProjectPlacement } from "../board/board-model";
import { mountDateFields, renderDateControl } from "../ui/date-field";
import { setPending } from "./dom";
import {
  CREATE_PROJECT_CODE_RACE,
  CREATE_PROJECT_INCOMPLETE,
  CREATE_PROJECT_MOVE_ILLEGAL_TITLE,
  CREATE_PROJECT_MOVE_REASON_MISSING,
  CREATE_PROJECT_MOVE_REFUSED_TITLE,
  CREATE_PROJECT_MOVE_UNKNOWN_DETAIL,
  CREATE_PROJECT_MOVE_UNKNOWN_TITLE,
  CREATE_PROJECT_VALUE_INVALID,
  createProjectDestinationTitle,
  createProjectDuplicateWarning,
  createProjectLandedDetail,
  createProjectMoveIllegalDetail,
  createProjectMoveRefusedDetail,
  createProjectPlacementFields,
  createProjectPlacementNote,
} from "./create-project-copy";
import { failureCopy, joinFields } from "./messages";

/**
 * Where the surface that opened the sheet wants the project to land.
 *
 * Derived from {@link ProjectPlacement} rather than re-declared, so the band that offers the
 * gesture and the sheet that performs it cannot drift: `blocked` is excluded because a blocked
 * band never opens this — its ghost card renders dead instead (`board-model.ts`).
 *
 * `null` is the ordinary case (`/projects`): create the project and let the workflow decide.
 */
export type CreateProjectPlacement = Exclude<
  ProjectPlacement,
  { kind: "blocked" }
>;

/**
 * The document event that opens the sheet.
 *
 * A `CustomEvent` on `document` rather than an exported `open()`, so no surface
 * has to import another to offer creation: the page header, the empty state and
 * anything added later all say the same sentence into the room, and exactly one
 * listener answers it.
 *
 * Its `detail` is the landing band, or `null` for a surface that has none. Typed through the
 * augmentation below rather than narrowed at the listener: `CustomEvent.detail` is `any` in
 * the DOM lib, and casting it back would be exactly the assertion `FRONTEND.md` §1 forbids.
 */
export const CREATE_PROJECT_EVENT = "aztec:create-project";

declare global {
  interface DocumentEventMap {
    [CREATE_PROJECT_EVENT]: CustomEvent<CreateProjectPlacement | null>;
  }
}

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

  /**
   * The band this sheet was opened for, held for the whole submission.
   *
   * Captured at open and never re-read from the DOM: the rail underneath can be repainted by
   * the stream while the form is being filled in, and a destination that changed under the
   * operator is a project landing somewhere nobody asked for.
   */
  let placement: CreateProjectPlacement | null = null;

  const open = (landing: CreateProjectPlacement | null): void => {
    if (dialog.open) return;
    acknowledged = null;
    placement = landing;
    resetForm(dialog, form, defaults);
    applyPlacement(dialog, landing);
    dialog.showModal();
    form.querySelector<HTMLInputElement>("[name='name']")?.focus();
  };

  document.addEventListener(
    CREATE_PROJECT_EVENT,
    (event) => {
      open(event.detail);
    },
    { signal },
  );

  document.addEventListener(
    "click",
    (event) => {
      const target = event.target;
      if (!(target instanceof Element)) return;
      if (target.closest("[data-action='open-create-project']") === null)
        return;
      open(null);
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
        void submit(dialog, form, acknowledged, placement);
      }
    },
    { signal },
  );

  form.addEventListener(
    "submit",
    (event) => {
      event.preventDefault();
      void submit(dialog, form, acknowledged, placement);
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
  placement: CreateProjectPlacement | null,
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

  // Asked here rather than after the POST, because the alternative is a project that exists
  // and a move refused for a field the form could have collected in the same breath.
  const moveReason = textValue(form, "move_reason");
  if (
    placement !== null &&
    placement.kind === "one-hop" &&
    placement.requiresReason &&
    moveReason === ""
  ) {
    showDuplicate(dialog, null);
    showError(dialog, CREATE_PROJECT_MOVE_REASON_MISSING);
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
  await land(created, placement, moveReason);

  // Reconciled with the server, never with our guess at where the project ended up: the board
  // re-reads the queue and draws the card in whichever band it is actually standing in.
  await navigate(
    placement === null
      ? window.location.href
      : `/board?project=${encodeURIComponent(created.code)}`,
  );
}

/**
 * Puts the created project in the band the operator pressed, and says what happened.
 *
 * Legality is re-checked against **the record**, not against the graph the band was drawn
 * from: `created.transitions` is what this project may do right now, while the workflow
 * catalog only says which arrows an operator drew (`docs/API.md` §2.2). An absent transition
 * is therefore not posted at all — a refusal we can see coming is not worth a request.
 *
 * Every arm announces, including the ones that failed halfway: the project exists in all of
 * them, so an operator left without a toast would go looking for a project they were never
 * told about.
 */
async function land(
  created: ProjectDetail,
  placement: CreateProjectPlacement | null,
  reason: string,
): Promise<void> {
  if (
    placement === null ||
    placement.kind === "initial" ||
    created.state.code === placement.stateCode
  ) {
    toast({
      kind: "success",
      title: "Proyecto creado",
      detail: createProjectLandedDetail(created.code, created.state.label),
    });
    return;
  }

  const move = created.transitions.find(
    (transition) => transition.to_state.code === placement.stateCode,
  );
  if (move === undefined) {
    toast({
      kind: "error",
      title: CREATE_PROJECT_MOVE_ILLEGAL_TITLE,
      detail: createProjectMoveIllegalDetail(created.code, created.state.label),
    });
    return;
  }

  const moved = await postProjectTransition(created.code, {
    to_state: placement.stateCode,
    reason,
  });
  if (moved.ok) {
    toast({
      kind: "success",
      title: "Proyecto creado",
      detail: createProjectLandedDetail(
        moved.data.code,
        moved.data.state.label,
      ),
    });
    return;
  }

  const error = moved.error;
  if (error.kind === "network") {
    toast({
      kind: "error",
      title: CREATE_PROJECT_MOVE_UNKNOWN_TITLE,
      detail: CREATE_PROJECT_MOVE_UNKNOWN_DETAIL,
    });
    return;
  }
  if (error.kind === "transition_not_allowed") {
    toast({
      kind: "error",
      title: CREATE_PROJECT_MOVE_ILLEGAL_TITLE,
      detail: createProjectMoveIllegalDetail(created.code, created.state.label),
    });
    return;
  }
  // A refusal naming fields is the common one — a motive the edge demanded, or an attribute
  // `requires_fields` names — so what is missing is quoted rather than summarised away.
  const missing =
    error.kind === "validation" ? joinFields(Object.keys(error.fields)) : "";
  toast({
    kind: "error",
    title: CREATE_PROJECT_MOVE_REFUSED_TITLE,
    detail:
      missing === ""
        ? `${createProjectMoveRefusedDetail(created.code, created.state.label, "")} ${failureCopy(error).detail}`
        : createProjectMoveRefusedDetail(
            created.code,
            created.state.label,
            missing,
          ),
  });
}

/**
 * Paints the destination into the sheet: its heading, its note, and the motive field.
 *
 * Everything is reset for `null` rather than left as it was, because one dialog serves both
 * doors on the board — a ghost card and the ordinary button — and a heading left over from the
 * previous open would name a band this project is not going to.
 */
function applyPlacement(
  dialog: HTMLDialogElement,
  placement: CreateProjectPlacement | null,
): void {
  const title = dialog.querySelector<HTMLElement>(
    "[data-field='create-project-title']",
  );
  if (title !== null) {
    title.textContent =
      placement === null
        ? (title.dataset["default"] ?? "")
        : createProjectDestinationTitle(placement.stateLabel);
  }

  const note = dialog.querySelector<HTMLElement>(
    "[data-field='create-project-placement']",
  );
  if (note !== null) {
    const sentences =
      placement === null || placement.kind !== "one-hop"
        ? []
        : [
            createProjectPlacementNote(
              placement.stateLabel,
              placement.transitionLabel,
            ),
            ...(placement.requiresFields.length === 0
              ? []
              : [
                  createProjectPlacementFields(
                    placement.stateLabel,
                    joinFields(placement.requiresFields),
                  ),
                ]),
          ];
    note.hidden = sentences.length === 0;
    note.textContent = sentences.join(" ");
  }

  const field = dialog.querySelector<HTMLElement>(
    "[data-field='create-project-move-reason']",
  );
  if (field !== null) {
    field.hidden =
      placement === null ||
      placement.kind !== "one-hop" ||
      !placement.requiresReason;
  }
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
