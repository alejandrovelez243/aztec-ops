/**
 * The `/workflows` editor: the island that turns a drawing of the operation into the place it is
 * shaped.
 *
 * **Nothing here is optimistic.** Every write goes to the API and the screen is repainted from
 * the graph the server answers with — which is the whole graph, re-read after the change, not an
 * echo of the request. So the diagram, the states and the moves are always three projections of
 * one document, and a refusal leaves the screen exactly as it was
 * (`docs/standards/PATTERNS_FRONTEND.md` §8: the server owns legality, and authoring rules —
 * "that state still has three projects on it" — are legality about the graph).
 *
 * **Every control is hidden until the session says ops lead, and that is a courtesy, not the
 * enforcement.** The server renders this page from a mirror cookie that carries a token rather
 * than a capability, so the reveal happens here; the API refuses the write either way, and when
 * it does, the toast names the capability that was missing instead of saying "403".
 *
 * There is no stream on this surface. Configuration does not arrive over the bus, so the arm set
 * is four — loading, ready, empty, error — and `disconnected` would be a staleness marker on
 * data no envelope can make stale.
 */
import {
  deleteWorkflowState,
  deleteWorkflowTransition,
  getWorkflowGuards,
  getWorkflows,
  patchWorkflow,
  patchWorkflowState,
  patchWorkflowTransition,
  postWorkflow,
  postWorkflowState,
  postWorkflowTransition,
} from "../../lib/api/client";
import type { Result } from "../../lib/api/client";
import type { WorkflowShape } from "../../lib/api/domain";
import type { ApiError } from "../../lib/api/errors";
import { errorCopyByCode } from "../../lib/api/error-copy";
import { getOperator } from "../../lib/auth/session";
import { cloneTemplate, setField } from "../../lib/dom/patch";
import { toast } from "../../lib/toast";
import { mountMenus } from "../../lib/ui/menu";
import { setPending } from "../projects/dom";
import {
  authoringErrorCopy,
  isWithdrawnTransition,
  parseFieldNames,
  formatFieldNames,
  tokenColor,
  colorToken,
  type Choice,
} from "./authoring";
import { buildWorkflowView, buildWorkflowViews } from "./graph";
import { paintWorkflow, revealControls } from "./paint";
import {
  findPicker,
  pickerValue,
  selectPicker,
  setPickerOptions,
} from "./picker";

/**
 * What the guard picker means by "leave it as it is".
 *
 * A move's `guard` is deliberately published on **no** read — whether it passes depends on facts
 * no row of the graph holds — so an edit form cannot show the current one, and defaulting the
 * control to "sin condición" would silently strip a safety check every time somebody fixed a
 * label. The sentinel omits the field instead; choosing "sin condición" explicitly clears it.
 * Guard codes are registry slugs, so nothing can collide with this.
 */
const GUARD_KEEP = "__sin_cambios__";

/** What a completed write says out loud; `detail` is the second line when there is one. */
interface Announcement {
  readonly title: string;
  readonly detail?: string | undefined;
}

/** Which workflow form is open, and what it will do on submit. */
type WorkflowMode =
  | { readonly kind: "create" }
  | { readonly kind: "rename"; readonly code: string };

/** Which state form is open. The two modes ask different things, so they are not one flag. */
type StateMode =
  | { readonly kind: "create"; readonly workflow: string }
  | {
      readonly kind: "edit";
      readonly workflow: string;
      readonly code: string;
    };

/** Which move form is open; an edit knows the pair it may not change. */
type MoveMode =
  | { readonly kind: "create"; readonly workflow: string }
  | {
      readonly kind: "edit";
      readonly workflow: string;
      readonly from: string;
      readonly to: string;
    };

/**
 * Wires the surface and returns its teardown.
 *
 * @param root - The `[data-workflows-region]` element the page rendered.
 */
export function mountWorkflowEditor(root: HTMLElement): () => void {
  const isOpsLead = getOperator()?.isOpsLead === true;
  const controller = new AbortController();
  const { signal } = controller;

  /**
   * The guard registry, read once and kept.
   *
   * `null` means "not asked yet". Fetched when the move dialog first opens rather than on mount:
   * a reader who never authors a transition should not pay for a request, and the list is
   * ops-lead-only anyway.
   */
  let guards: readonly string[] | null = null;

  let workflowMode: WorkflowMode = { kind: "create" };
  let stateMode: StateMode = { kind: "create", workflow: "" };
  let moveMode: MoveMode = { kind: "create", workflow: "" };

  /** The control that started the write, so focus returns to it after the repaint. */
  let focusKey: string | null = null;

  const find = <T extends HTMLElement>(selector: string): T | null =>
    root.querySelector<T>(selector);

  const workflowDialog = (): HTMLDialogElement | null =>
    find<HTMLDialogElement>("[data-workflow-dialog]");
  const stateDialog = (): HTMLDialogElement | null =>
    find<HTMLDialogElement>("[data-state-dialog]");
  const moveDialog = (): HTMLDialogElement | null =>
    find<HTMLDialogElement>("[data-move-dialog]");

  const sectionOf = (code: string): HTMLElement | null =>
    find(`[data-workflow][data-code="${CSS.escape(code)}"]`);

  /** What one picker of a form currently holds; `""` when the form has no such picker. */
  const pick = (scope: ParentNode, facet: string): string => {
    const control = findPicker(scope, facet);
    return control === null ? "" : pickerValue(control);
  };

  function setArm(arm: "loading" | "ready" | "empty" | "error"): void {
    root.dataset["view"] = arm;
  }

  // --- Reading -----------------------------------------------------------------

  /**
   * Re-reads every graph and rebuilds the list.
   *
   * Only two paths need it — the retry on the error arm, and the mount that follows a failed
   * server render — because every *write* already answers with its own graph. A read that fails
   * lands on the error arm with the reason spelled out, never on a blank page.
   */
  async function readAll(): Promise<void> {
    const result = await getWorkflows();
    if (!result.ok) {
      showReadFailure(result.error);
      return;
    }
    const views = buildWorkflowViews(result.data);
    const graphs = find("[data-graphs-arm]");
    if (graphs === null) return;

    const kept = new Set<string>();
    for (const view of views) {
      const section = sectionOf(view.code) ?? cloneSection();
      if (section === null) continue;
      paintWorkflow(section, view, { opsLead: isOpsLead });
      graphs.append(section);
      kept.add(view.code);
    }
    for (const section of root.querySelectorAll<HTMLElement>(
      "[data-workflow]",
    )) {
      if (!kept.has(section.dataset["code"] ?? "")) section.remove();
    }
    setArm(views.length === 0 ? "empty" : "ready");
  }

  /** Writes the failure into the error arm and shows it; the retry re-runs the read. */
  function showReadFailure(error: ApiError): void {
    const copy = errorCopyByCode(error.code);
    setField(root, "wf-read-error-title", copy.title);
    setField(root, "wf-read-error-detail", copy.detail);
    setArm("error");
  }

  /** A blank section from the page's template, ready to be painted. */
  function cloneSection(): HTMLElement | null {
    return cloneTemplate(root, "workflow-section");
  }

  /**
   * Applies one graph the API just answered with.
   *
   * The section is created when the graph is new — which is what a freshly created lifecycle is
   * — so the operator sees it appear rather than being told to reload.
   */
  function applyShape(shape: WorkflowShape): HTMLElement | null {
    const view = buildWorkflowView(shape);
    let section = sectionOf(shape.code);
    if (section === null) {
      section = cloneSection();
      if (section === null) return null;
      find("[data-graphs-arm]")?.append(section);
    }
    paintWorkflow(section, view, { opsLead: isOpsLead });
    setArm("ready");
    return section;
  }

  // --- Writing -----------------------------------------------------------------

  /**
   * Runs one write with its control in flight, then repaints from the answer.
   *
   * Every write on this surface goes through here, which is what makes the four promises the
   * same everywhere: the pressed control spins and stops accepting input, the outcome is
   * announced, the screen is repainted from the server's own re-read, and focus comes back to
   * the control that is no longer the same element.
   */
  async function submit(
    button: HTMLButtonElement | null,
    run: () => Promise<Result<WorkflowShape>>,
    announce: (shape: WorkflowShape) => Announcement,
    onFailure?: (error: ApiError) => void,
  ): Promise<boolean> {
    if (button !== null) setPending(button, true);
    const result = await run();
    if (button !== null) setPending(button, false);

    if (!result.ok) {
      const copy = authoringErrorCopy(result.error);
      toast({ kind: "error", title: copy.title, detail: copy.detail });
      onFailure?.(result.error);
      return false;
    }

    applyShape(result.data);
    restoreFocus();
    const message = announce(result.data);
    toast({
      kind: "success",
      title: message.title,
      ...(message.detail === undefined ? {} : { detail: message.detail }),
    });
    return true;
  }

  /**
   * Returns focus to the repainted twin of the control that was pressed.
   *
   * Called twice on the dialog paths, and deliberately: the paint replaces the row the operator
   * came from, and then `dialog.close()` hands focus back to an element that no longer exists.
   * The key is cleared by the next click, not by this, so the second call is the one that sticks.
   */
  function restoreFocus(): void {
    if (focusKey === null) return;
    const target = find<HTMLElement>(
      `[data-focus-key="${CSS.escape(focusKey)}"]`,
    );
    if (target instanceof HTMLButtonElement && !target.disabled) target.focus();
  }

  /** Closes a dialog and puts focus back where the operator left it. */
  function closeDialog(dialog: HTMLDialogElement | null): void {
    dialog?.close();
    restoreFocus();
  }

  // --- Confirmation ------------------------------------------------------------

  /**
   * Asks in the product's own dialog and resolves with what the operator chose.
   *
   * The product's own `<dialog>` rather than `window.confirm`, which the browser draws in its
   * own chrome with English buttons and a `localhost:4321 says` title.
   *
   * It resolves from **the answer**, not only from the dialog's `close` event, and that is a
   * scar: `close` is not delivered in every environment this runs in, and a confirmation whose
   * promise never settles turns "retirar" into a button that silently does nothing — the worst
   * possible failure for a destructive action. So the two answers are read from the controls
   * that give them, `Escape` is answered directly, and the `close` event is kept as a third
   * route for whatever dismisses the dialog some other way. Whichever arrives first settles; the
   * rest are unhooked.
   *
   * A dialog absent from the DOM resolves `false`: a confirmation that cannot be shown must
   * refuse the action, never assume it.
   */
  function confirmAction(
    title: string,
    body: string,
    cta: string,
  ): Promise<boolean> {
    const dialog = find<HTMLDialogElement>("[data-confirm-dialog]");
    if (dialog === null) return Promise.resolve(false);
    setField(dialog, "confirm-title", title);
    setField(dialog, "confirm-body", body);
    setField(dialog, "confirm-cta", cta);
    dialog.returnValue = "";

    return new Promise<boolean>((resolve) => {
      const settle = (answer: boolean): void => {
        dialog.removeEventListener("click", onDialogClick);
        dialog.removeEventListener("keydown", onKey);
        dialog.removeEventListener("close", onClose);
        if (dialog.open) dialog.close();
        resolve(answer);
      };
      const onDialogClick = (event: MouseEvent): void => {
        const target = event.target;
        if (!(target instanceof Element)) return;
        if (target.closest('[data-action="submit-confirm"]') !== null) {
          // The dialog is closed by `settle`, so the form's own submission is not needed and
          // would race it.
          event.preventDefault();
          settle(true);
          return;
        }
        if (target.closest('[data-action="close-dialog"]') !== null)
          settle(false);
      };
      const onKey = (event: KeyboardEvent): void => {
        if (event.key === "Escape") settle(false);
      };
      const onClose = (): void => settle(dialog.returnValue === "confirm");

      dialog.addEventListener("click", onDialogClick);
      dialog.addEventListener("keydown", onKey);
      dialog.addEventListener("close", onClose);
      dialog.showModal();
    });
  }

  // --- The workflow form -------------------------------------------------------

  function openWorkflowDialog(mode: WorkflowMode): void {
    const dialog = workflowDialog();
    const form = dialog?.querySelector<HTMLFormElement>("[data-workflow-form]");
    if (dialog === null || form === undefined || form === null) return;

    workflowMode = mode;
    form.reset();
    showError(form, "wf-error", null);

    const isCreate = mode.kind === "create";
    setField(
      form,
      "wf-dialog-title",
      isCreate ? "Nuevo flujo" : "Renombrar el flujo",
    );
    setField(
      form,
      "wf-dialog-lead",
      isCreate
        ? "Un flujo nace vacío: primero se crea, después se le añaden los estados y los movimientos, uno a uno."
        : "El código y la clase de registro no se tocan: son la dirección del flujo y lo que gobierna. El nombre sí.",
    );
    setField(form, "wf-submit", isCreate ? "Crear flujo" : "Guardar el nombre");
    toggleField(form, "wf-code-field", isCreate);
    toggleField(form, "wf-applies-field", isCreate);
    toggleField(form, "wf-types-field", isCreate);
    toggleField(form, "wf-code-line", !isCreate);

    if (mode.kind === "rename") {
      const section = sectionOf(mode.code);
      setField(form, "wf-code-value", mode.code);
      const name = form.elements.namedItem("name");
      if (name instanceof HTMLInputElement) {
        name.value = section?.dataset["name"] ?? "";
      }
    }

    const applies = findPicker(form, "wf-applies");
    if (applies !== null) selectPicker(applies, "PROJECT");

    dialog.showModal();
    focusFirstField(form);
  }

  async function submitWorkflow(form: HTMLFormElement): Promise<void> {
    const data = new FormData(form);
    const name = String(data.get("name") ?? "").trim();
    const button = form.querySelector<HTMLButtonElement>(
      '[data-action="submit-workflow"]',
    );
    if (name === "") {
      showError(form, "wf-error", "Escribe un nombre para el flujo.");
      return;
    }

    if (workflowMode.kind === "rename") {
      const code = workflowMode.code;
      const done = await submit(
        button,
        () => patchWorkflow(code, { name }),
        (shape) => ({ title: `El flujo ahora se llama «${shape.name}»` }),
        (error) =>
          showError(form, "wf-error", authoringErrorCopy(error).detail),
      );
      if (done) closeDialog(workflowDialog());
      return;
    }

    const code = String(data.get("code") ?? "").trim();
    if (code === "") {
      showError(form, "wf-error", "Escribe un código para el flujo.");
      return;
    }
    const appliesTo = pick(form, "wf-applies");
    const types = data
      .getAll("engagement_types")
      .map((value) => String(value))
      .filter((value) => value !== "");

    const done = await submit(
      button,
      () =>
        postWorkflow({
          code,
          name,
          applies_to: appliesTo === "" ? "PROJECT" : appliesTo,
          ...(types.length === 0 ? {} : { engagement_types: types }),
        }),
      (shape) => ({
        title: `Flujo «${shape.name}» creado`,
        detail:
          "Está vacío: sin estados no puede recibir trabajo, así que el siguiente paso es su primer estado.",
      }),
      (error) => showError(form, "wf-error", authoringErrorCopy(error).detail),
    );
    if (!done) return;

    closeDialog(workflowDialog());
    // The walk-through: a brand new graph is a blank diagram, and leaving the operator in front
    // of one is leaving them at a dead end. The section is scrolled to and the first state is
    // already being asked for.
    sectionOf(code)?.scrollIntoView({ block: "start", behavior: "smooth" });
    openStateDialog({ kind: "create", workflow: code });
  }

  // --- The state form ----------------------------------------------------------

  function openStateDialog(mode: StateMode): void {
    const dialog = stateDialog();
    const form = dialog?.querySelector<HTMLFormElement>("[data-state-form]");
    if (dialog === null || form === undefined || form === null) return;

    stateMode = mode;
    form.reset();
    showError(form, "state-error", null);

    const isCreate = mode.kind === "create";
    const section = sectionOf(mode.workflow);
    const workflowName = section?.dataset["name"] ?? mode.workflow;
    const isFirst = section?.querySelector("[data-state-row]") === null;

    setField(
      form,
      "state-dialog-title",
      isCreate ? `Nuevo estado en ${workflowName}` : "Editar el estado",
    );
    setField(
      form,
      "state-dialog-lead",
      isCreate && isFirst
        ? "El primer estado de un flujo vacío se convierte en su entrada: es donde aterriza todo el trabajo nuevo."
        : isCreate
          ? "Se añade al final del orden actual salvo que digas otra cosa."
          : "El código no cambia: lo llevan encima los registros que pasaron por aquí. La etiqueta sí.",
    );
    setField(
      form,
      "state-submit",
      isCreate ? "Añadir estado" : "Guardar el estado",
    );
    toggleField(form, "state-code-field", isCreate);
    toggleField(form, "state-code-line", !isCreate);

    const category = findPicker(form, "state-category");
    const row =
      mode.kind === "edit"
        ? section?.querySelector<HTMLElement>(
            `[data-state-row][data-code="${CSS.escape(mode.code)}"]`,
          )
        : null;

    if (mode.kind === "edit" && row !== null && row !== undefined) {
      setField(form, "state-code-value", mode.code);
      const label = form.elements.namedItem("label");
      if (label instanceof HTMLInputElement) {
        label.value = row.dataset["stateLabel"] ?? "";
      }
      const order = form.elements.namedItem("order");
      if (order instanceof HTMLInputElement) {
        order.value = row.dataset["order"] ?? "";
      }
      if (category !== null) {
        selectPicker(category, row.dataset["category"] ?? "BACKLOG");
      }
      selectColor(form, colorToken(row.dataset["color"] ?? ""));
      setField(
        form,
        "state-order-hint",
        "Cambia el número para moverlo dentro del flujo.",
      );
    } else {
      if (category !== null) selectPicker(category, "BACKLOG");
      selectColor(form, "");
      setField(
        form,
        "state-order-hint",
        "Déjalo vacío para añadirlo al final.",
      );
    }

    dialog.showModal();
    focusFirstField(form);
  }

  async function submitState(form: HTMLFormElement): Promise<void> {
    const data = new FormData(form);
    const label = String(data.get("label") ?? "").trim();
    const category = pick(form, "state-category");
    const order = readOrder(data.get("order"));
    const color = tokenColor(String(data.get("color") ?? ""));
    const button = form.querySelector<HTMLButtonElement>(
      '[data-action="submit-state"]',
    );

    if (label === "") {
      showError(form, "state-error", "Escribe una etiqueta para el estado.");
      return;
    }
    if (order === "invalid") {
      showError(
        form,
        "state-error",
        "El orden tiene que ser un número entero de 0 en adelante.",
      );
      return;
    }

    const mode = stateMode;
    const fail = (error: ApiError): void =>
      showError(form, "state-error", authoringErrorCopy(error).detail);

    if (mode.kind === "edit") {
      const done = await submit(
        button,
        () =>
          patchWorkflowState(mode.workflow, mode.code, {
            label,
            category,
            color,
            ...(order === null ? {} : { order }),
          }),
        () => ({ title: `Estado «${label}» actualizado` }),
        fail,
      );
      if (done) closeDialog(stateDialog());
      return;
    }

    const code = String(data.get("code") ?? "").trim();
    if (code === "") {
      showError(form, "state-error", "Escribe un código para el estado.");
      return;
    }
    const done = await submit(
      button,
      () =>
        postWorkflowState(mode.workflow, {
          code,
          label,
          category,
          color,
          ...(order === null ? {} : { order }),
        }),
      (shape) => ({
        title: `Estado «${label}» añadido`,
        detail:
          shape.states.filter((state) => state.is_active).length === 1
            ? "Es el estado de entrada del flujo: aquí aterriza el trabajo nuevo."
            : shape.transitions.length === 0
              ? "Todavía no hay ningún movimiento entre estados: declara uno para que el trabajo pueda avanzar."
              : undefined,
      }),
      fail,
    );
    if (done) closeDialog(stateDialog());
  }

  /** Retires a state, asking first — and naming what retiring actually does. */
  async function retireState(
    section: HTMLElement,
    row: HTMLElement,
  ): Promise<void> {
    const code = row.dataset["code"] ?? "";
    const label = row.dataset["stateLabel"] ?? code;
    const workflow = section.dataset["code"] ?? "";
    const confirmed = await confirmAction(
      `¿Retirar «${label}»?`,
      "Sale del flujo y se retiran también los movimientos que entran o salen de él. No se borra nada: los registros que pasaron por aquí lo siguen nombrando, y puedes restaurarlo desde su fila.",
      "Retirar el estado",
    );
    if (!confirmed) return;

    await submit(
      row.querySelector<HTMLButtonElement>('[data-state-action="retire"]'),
      () => deleteWorkflowState(workflow, code),
      () => ({
        title: `Estado «${label}» retirado`,
        detail: "Los movimientos que lo tocaban quedaron retirados con él.",
      }),
    );
  }

  /** Puts a retired state back in the graph; always safe, so nothing is asked. */
  async function restoreState(
    section: HTMLElement,
    row: HTMLElement,
  ): Promise<void> {
    const code = row.dataset["code"] ?? "";
    const label = row.dataset["stateLabel"] ?? code;
    await submit(
      row.querySelector<HTMLButtonElement>('[data-state-action="restore"]'),
      () =>
        patchWorkflowState(section.dataset["code"] ?? "", code, {
          is_active: true,
        }),
      () => ({
        title: `Estado «${label}» restaurado`,
        detail:
          "Vuelve al flujo sin movimientos: los que se retiraron con él se restauran uno a uno.",
      }),
    );
  }

  // --- The move form -----------------------------------------------------------

  async function openMoveDialog(mode: MoveMode): Promise<void> {
    const dialog = moveDialog();
    const form = dialog?.querySelector<HTMLFormElement>("[data-move-form]");
    if (dialog === null || form === undefined || form === null) return;

    moveMode = mode;
    form.reset();
    showError(form, "move-error", null);
    toggleField(form, "move-restore", false);

    const isCreate = mode.kind === "create";
    const section = sectionOf(mode.workflow);
    setField(
      form,
      "move-dialog-title",
      isCreate ? "Nuevo movimiento" : "Editar el movimiento",
    );
    setField(
      form,
      "move-dialog-lead",
      isCreate
        ? "Declarar un movimiento no cambia ningún registro: abre el paso desde el estado de origen, y cada proyecto o tarea sigue decidiéndose por separado."
        : "Los dos extremos son la dirección del movimiento y no se editan. Lo que pide sí.",
    );
    setField(
      form,
      "move-submit",
      isCreate ? "Declarar movimiento" : "Guardar el movimiento",
    );
    toggleField(form, "move-ends-field", isCreate);
    toggleField(form, "move-ends-line", !isCreate);

    const options = stateChoices(section);
    const from = findPicker(form, "move-from");
    const to = findPicker(form, "move-to");
    if (from !== null) setPickerOptions(from, options, "");
    if (to !== null) setPickerOptions(to, options, "");

    if (mode.kind === "edit") {
      const row = section?.querySelector<HTMLElement>(
        `[data-move-row][data-from="${CSS.escape(mode.from)}"][data-to="${CSS.escape(mode.to)}"]`,
      );
      setField(form, "move-from-value", mode.from);
      setField(form, "move-to-value", mode.to);
      const label = form.elements.namedItem("label");
      if (label instanceof HTMLInputElement) {
        label.value = row?.dataset["moveLabel"] ?? "";
      }
      const reason = form.elements.namedItem("requires_reason");
      if (reason instanceof HTMLInputElement) {
        reason.checked = row?.dataset["requiresReason"] === "true";
      }
      const fields = form.elements.namedItem("requires_fields");
      if (fields instanceof HTMLInputElement) {
        fields.value = formatFieldNames(
          parseFieldNames(row?.dataset["requiresFields"] ?? ""),
        );
      }
    }

    dialog.showModal();
    focusFirstField(form);
    await fillGuards(form, mode.kind === "edit");
  }

  /** The states a move may join: the active ones, named the way the tables name them. */
  function stateChoices(section: HTMLElement | null): readonly Choice[] {
    if (section === null) return [];
    return [
      ...section.querySelectorAll<HTMLElement>(
        '[data-state-row][data-active="true"]',
      ),
    ].map((row) => ({
      value: row.dataset["code"] ?? "",
      label: row.dataset["stateLabel"] ?? row.dataset["code"] ?? "",
    }));
  }

  /**
   * Fills the guard picker, reading the registry the first time it is needed.
   *
   * A failed read leaves the control with only its two structural entries and says so, rather
   * than blocking the form: a move without a guard is a legitimate move, and refusing to let
   * anybody declare one because a secondary list did not load would be the tail wagging the dog.
   */
  async function fillGuards(
    form: HTMLFormElement,
    isEdit: boolean,
  ): Promise<void> {
    const picker = findPicker(form, "move-guard");
    if (picker === null) return;

    if (guards === null) {
      const result = await getWorkflowGuards();
      guards = result.ok ? result.data : [];
      if (!result.ok) {
        toast({
          kind: "info",
          title: "No pudimos leer las condiciones disponibles",
          detail:
            "Puedes declarar el movimiento sin condición; vuelve a abrir el formulario para reintentarlo.",
        });
      }
    }

    const known = guards ?? [];
    const options: Choice[] = [
      ...(isEdit ? [{ value: GUARD_KEEP, label: "Sin cambios" }] : []),
      { value: "", label: "Sin condición" },
      ...known.map((code) => ({ value: code, label: code })),
    ];
    setPickerOptions(picker, options, isEdit ? GUARD_KEEP : "");
  }

  async function submitMove(form: HTMLFormElement): Promise<void> {
    const data = new FormData(form);
    const label = String(data.get("label") ?? "").trim();
    const requiresReason = data.get("requires_reason") !== null;
    const requiresFields = parseFieldNames(
      String(data.get("requires_fields") ?? ""),
    );
    const order = readOrder(data.get("order"));
    const guard = pick(form, "move-guard");
    const button = form.querySelector<HTMLButtonElement>(
      '[data-action="submit-move"]',
    );

    if (label === "") {
      showError(
        form,
        "move-error",
        "Escribe la etiqueta del movimiento: es el texto del botón.",
      );
      return;
    }
    if (order === "invalid") {
      showError(
        form,
        "move-error",
        "El orden tiene que ser un número entero de 0 en adelante.",
      );
      return;
    }

    const mode = moveMode;
    const fail = (error: ApiError): void => {
      showError(form, "move-error", authoringErrorCopy(error).detail);
      // The one refusal with a way out that is not "change what you typed": the pair exists but
      // is withdrawn, so it is invisible everywhere else and can only come back from here.
      toggleField(form, "move-restore", isWithdrawnTransition(error));
    };

    if (mode.kind === "edit") {
      const done = await submit(
        button,
        () =>
          patchWorkflowTransition(mode.workflow, mode.from, mode.to, {
            label,
            requires_reason: requiresReason,
            requires_fields: requiresFields,
            ...(guard === GUARD_KEEP ? {} : { guard }),
            ...(order === null ? {} : { order }),
          }),
        () => ({ title: `Movimiento «${label}» actualizado` }),
        fail,
      );
      if (done) closeDialog(moveDialog());
      return;
    }

    const from = pick(form, "move-from");
    const to = pick(form, "move-to");
    if (from === "" || to === "") {
      showError(
        form,
        "move-error",
        "Elige el estado de origen y el de destino.",
      );
      return;
    }
    if (from === to) {
      showError(
        form,
        "move-error",
        "Un movimiento une dos estados distintos: el origen y el destino no pueden ser el mismo.",
      );
      return;
    }

    const done = await submit(
      button,
      () =>
        postWorkflowTransition(mode.workflow, {
          from_state: from,
          to_state: to,
          label,
          requires_reason: requiresReason,
          requires_fields: requiresFields,
          guard: guard === GUARD_KEEP ? "" : guard,
          ...(order === null ? {} : { order }),
        }),
      () => ({ title: `Movimiento «${label}» declarado` }),
      fail,
    );
    if (done) closeDialog(moveDialog());
  }

  /** Brings back a withdrawn move, carrying whatever the operator had just typed for it. */
  async function restoreMove(form: HTMLFormElement): Promise<void> {
    const mode = moveMode;
    if (mode.kind !== "create") return;
    const data = new FormData(form);
    const from = pick(form, "move-from");
    const to = pick(form, "move-to");
    const label = String(data.get("label") ?? "").trim();
    const guard = pick(form, "move-guard");
    const order = readOrder(data.get("order"));
    if (from === "" || to === "") return;
    // The same guard `submitState` and `submitMove` already apply. Without it a
    // restore carries `"invalid"` straight into the request body, which the API
    // rejects for a reason the operator never sees.
    if (order === "invalid") {
      showError(
        form,
        "move-error",
        "El orden tiene que ser un número entero de 0 en adelante.",
      );
      return;
    }

    const done = await submit(
      form.querySelector<HTMLButtonElement>('[data-action="restore-move"]'),
      () =>
        patchWorkflowTransition(mode.workflow, from, to, {
          is_active: true,
          label,
          requires_reason: data.get("requires_reason") !== null,
          requires_fields: parseFieldNames(
            String(data.get("requires_fields") ?? ""),
          ),
          ...(guard === GUARD_KEEP ? {} : { guard }),
          ...(order === null ? {} : { order }),
        }),
      () => ({
        title: `Movimiento «${label}» restaurado`,
        detail: "Vuelve al grafo con lo que acabas de escribir.",
      }),
      (error) =>
        showError(form, "move-error", authoringErrorCopy(error).detail),
    );
    if (done) closeDialog(moveDialog());
  }

  /** Withdraws a move, asking first — the graph loses an arrow that records may be using. */
  async function withdrawMove(
    section: HTMLElement,
    row: HTMLElement,
  ): Promise<void> {
    const from = row.dataset["from"] ?? "";
    const to = row.dataset["to"] ?? "";
    const label = row.dataset["moveLabel"] ?? `${from} → ${to}`;
    const confirmed = await confirmAction(
      `¿Retirar «${label}»?`,
      "Deja de ofrecerse a partir de ahora y desaparece del diagrama. Nada se borra: la bitácora sigue nombrándolo, y si era la última salida de su estado, ese estado pasa a ser un final del flujo.",
      "Retirar el movimiento",
    );
    if (!confirmed) return;

    await submit(
      row.querySelector<HTMLButtonElement>('[data-move-action="withdraw"]'),
      () => deleteWorkflowTransition(section.dataset["code"] ?? "", from, to),
      () => ({ title: `Movimiento «${label}» retirado` }),
    );
  }

  // --- The workflow's own switch -----------------------------------------------

  async function toggleWorkflow(section: HTMLElement): Promise<void> {
    const code = section.dataset["code"] ?? "";
    const name = section.dataset["name"] ?? code;
    const wasActive = section.dataset["active"] === "true";

    if (wasActive) {
      const confirmed = await confirmAction(
        `¿Desactivar «${name}»?`,
        "Deja de ofrecerse para trabajo nuevo. Todo lo que ya está dentro sigue rigiéndose por él y conserva sus movimientos: desactivar no es borrar, y puedes volver a activarlo cuando quieras.",
        "Desactivar el flujo",
      );
      if (!confirmed) return;
    }

    await submit(
      section.querySelector<HTMLButtonElement>(
        '[data-wf-action="toggle-active"]',
      ),
      () => patchWorkflow(code, { is_active: !wasActive }),
      () => ({
        title: wasActive
          ? `«${name}» quedó fuera de servicio`
          : `«${name}» vuelve a estar en servicio`,
      }),
    );
  }

  // --- Wiring ------------------------------------------------------------------

  function onClick(event: MouseEvent): void {
    const target = event.target;
    if (!(target instanceof Element)) return;

    const pressed = target.closest<HTMLElement>("[data-focus-key]");
    focusKey = pressed?.dataset["focusKey"] ?? null;

    if (target.closest('[data-action="close-dialog"]') !== null) {
      target.closest("dialog")?.close();
      return;
    }
    if (target.closest('[data-action="restore-move"]') !== null) {
      const form = target
        .closest("dialog")
        ?.querySelector<HTMLFormElement>("[data-move-form]");
      if (form !== null && form !== undefined) void restoreMove(form);
      return;
    }

    if (target.closest("[data-workflow-new]") !== null) {
      openWorkflowDialog({ kind: "create" });
      return;
    }
    if (target.closest("[data-workflows-retry]") !== null) {
      setArm("loading");
      void readAll();
      return;
    }

    const section = target.closest<HTMLElement>("[data-workflow]");
    if (section === null) return;

    const stateAction = target.closest<HTMLElement>("[data-state-action]");
    if (stateAction !== null) {
      const row = stateAction.closest<HTMLElement>("[data-state-row]");
      if (row !== null) {
        handleStateAction(
          stateAction.dataset["stateAction"] ?? "",
          section,
          row,
        );
      }
      return;
    }

    const moveAction = target.closest<HTMLElement>("[data-move-action]");
    if (moveAction !== null) {
      const row = moveAction.closest<HTMLElement>("[data-move-row]");
      if (row !== null) {
        handleMoveAction(moveAction.dataset["moveAction"] ?? "", section, row);
      }
      return;
    }

    const wfAction = target.closest<HTMLElement>("[data-wf-action]");
    if (wfAction !== null) {
      handleWorkflowAction(wfAction.dataset["wfAction"] ?? "", section);
    }
  }

  function handleWorkflowAction(action: string, section: HTMLElement): void {
    const code = section.dataset["code"] ?? "";
    if (action === "rename") {
      openWorkflowDialog({ kind: "rename", code });
      return;
    }
    if (action === "add-state") {
      openStateDialog({ kind: "create", workflow: code });
      return;
    }
    if (action === "add-transition") {
      void openMoveDialog({ kind: "create", workflow: code });
      return;
    }
    if (action !== "toggle-active") return;
    void toggleWorkflow(section);
  }

  function handleStateAction(
    action: string,
    section: HTMLElement,
    row: HTMLElement,
  ): void {
    if (action === "edit") {
      openStateDialog({
        kind: "edit",
        workflow: section.dataset["code"] ?? "",
        code: row.dataset["code"] ?? "",
      });
      return;
    }
    if (action === "retire") {
      void retireState(section, row);
      return;
    }
    if (action !== "restore") return;
    void restoreState(section, row);
  }

  function handleMoveAction(
    action: string,
    section: HTMLElement,
    row: HTMLElement,
  ): void {
    if (action === "edit") {
      void openMoveDialog({
        kind: "edit",
        workflow: section.dataset["code"] ?? "",
        from: row.dataset["from"] ?? "",
        to: row.dataset["to"] ?? "",
      });
      return;
    }
    if (action !== "withdraw") return;
    void withdrawMove(section, row);
  }

  function onSubmit(event: SubmitEvent): void {
    const form = event.target;
    if (!(form instanceof HTMLFormElement)) return;
    if (form.matches("[data-workflow-form]")) {
      event.preventDefault();
      void submitWorkflow(form);
      return;
    }
    if (form.matches("[data-state-form]")) {
      event.preventDefault();
      void submitState(form);
      return;
    }
    if (form.matches("[data-move-form]")) {
      event.preventDefault();
      void submitMove(form);
    }
  }

  root.addEventListener("click", onClick, { signal });
  root.addEventListener("submit", onSubmit, { signal });

  const releaseMenus = mountMenus(root, "[data-graph-picker]", {
    // Every option is already in the panel: the graph-shaped ones were written when the dialog
    // opened, and the closed vocabularies were server-rendered.
    fill: () => Promise.resolve(),
    choose: (control, item) => {
      selectPicker(control, item.dataset["value"] ?? "");
      return Promise.resolve();
    },
  });

  revealControls(root, isOpsLead);

  // The server render carries no session — the credential lives in localStorage, which SSR
  // cannot read — so a first paint that failed is retried here, where the token exists.
  if (root.dataset["view"] === "error") {
    setArm("loading");
    void readAll();
  }

  return () => {
    controller.abort();
    releaseMenus();
    for (const dialog of root.querySelectorAll("dialog")) {
      if (dialog.open) dialog.close();
    }
  };
}

/** Shows or clears one form's message; `null` hides it. */
function showError(
  form: ParentNode,
  field: string,
  message: string | null,
): void {
  const slot = form.querySelector<HTMLElement>(`[data-field="${field}"]`);
  if (slot === null) return;
  slot.textContent = message ?? "";
  slot.hidden = message === null;
}

/** Shows or hides one field of a form, by its `data-field` hook. */
function toggleField(form: ParentNode, field: string, visible: boolean): void {
  const slot = form.querySelector<HTMLElement>(`[data-field="${field}"]`);
  if (slot !== null) slot.hidden = !visible;
}

/**
 * Moves focus to the first thing a reopened form asks for.
 *
 * Pickers count: on "nuevo movimiento" the first question is which state the arrow leaves, and
 * that control is a `<button>`. Fields inside a hidden wrapper are skipped — the code input is
 * still in the DOM on an edit, it is simply not being asked for.
 */
function focusFirstField(form: HTMLFormElement): void {
  const controls = [
    ...form.querySelectorAll<HTMLElement>(
      "input:not([type='hidden']), [data-menu-trigger]",
    ),
  ];
  const first = controls.find(
    (control) =>
      control.closest("[hidden]") === null &&
      !(control instanceof HTMLInputElement && control.type === "radio"),
  );
  first?.focus();
}

/**
 * Reads the optional `order` field.
 *
 * Three answers rather than a number: `null` is "the operator left it empty", which the API reads
 * as "append", and `"invalid"` is text that is not a position — which must be caught here,
 * because sending it would turn a typo into a 422 about a field the form could have explained
 * itself.
 */
function readOrder(
  value: FormDataEntryValue | null,
): number | null | "invalid" {
  const raw = String(value ?? "").trim();
  if (raw === "") return null;
  const parsed = Number(raw);
  if (!Number.isInteger(parsed) || parsed < 0 || parsed > 32767)
    return "invalid";
  return parsed;
}

/** Ticks the swatch of a colour token; `""` ticks "the one from its category". */
function selectColor(form: HTMLFormElement, token: string): void {
  for (const input of form.querySelectorAll<HTMLInputElement>(
    "input[name='color']",
  )) {
    input.checked = input.value === token;
  }
}
