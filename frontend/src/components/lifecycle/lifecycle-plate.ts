/**
 * The lifecycle plate's behaviour: reveal who may change it, offer what may be chosen, and repaint
 * only from the server's answer.
 *
 * Three rules shape this module, and each is a decision rather than a style.
 *
 * **Nothing is painted before the write is confirmed.** A reassignment is refusable — the target
 * graph may not contain the state the record stands on, it may be retired, the caller may not be an
 * ops lead — so the plate keeps saying what the last authoritative read said until the response
 * arrives, and then the *whole record* is repainted from it. That is why success goes through
 * `applyProjectDetail` / `applyTaskDetail` rather than a local patch: the response is the record
 * re-read after the write, its `transitions` come from the new graph, and a bar still showing the
 * old graph's buttons would be offering moves that no longer exist.
 *
 * **A refusal this client can predict is prevented, not reported.** The catalog publishes every
 * graph's states, so an option the server would reject renders unchoosable with the missing state
 * named (`presentation.ts`, `fitOf`). The prediction hides a choice; it never grants one — the
 * request still goes out and the server is still the authority, which is what keeps this honest on
 * the day the two disagree.
 *
 * **A refusal that does arrive lands in the dialog, not in a toast.** The operator is looking at the
 * dialog, the dialog stays open, and the next option is one key away. Only the outcome — the change
 * that happened — is announced as a toast, because by then the dialog is gone.
 */

import {
  deleteProjectWorkflow,
  deleteTaskWorkflow,
  getWorkflows,
  putProjectWorkflow,
  putTaskWorkflow,
  type Result,
} from "../../lib/api/client";
import { getOperator } from "../../lib/auth/session";
import { shake } from "../../lib/motion/spring";
import { toast } from "../../lib/toast";
import { cloneTemplate, setField, setPending } from "../projects/dom";
import { applyProjectDetail } from "../projects/project-paint";
import { applyTaskDetail } from "../tasks/task-paint";
import type { LifecycleRecord } from "./paint";
import {
  buildOptions,
  currentChoice,
  refusalCopy,
  sameChoice,
  scopeCopy,
  toScope,
  type LifecycleChoice,
  type LifecycleContext,
  type LifecycleOption,
  type LifecycleScope,
  type RefusalCopy,
} from "./presentation";
import type {
  ProjectDetail,
  TaskDetail,
  WorkflowShape,
} from "../../lib/api/domain";

/**
 * How one kind of record is reassigned and repainted.
 *
 * A record per scope rather than an `if (scope === "project")` at three call sites: a third kind of
 * record is one entry here and no new branch, which is the shape rule the backend keeps for signals
 * and risk criteria (CLAUDE.md rule 8). Each entry closes over its own painter, so the two response
 * types never have to be narrowed at the call site.
 */
interface Reassigner {
  /** Puts the record on a named graph — `PUT .../workflow`. */
  readonly assign: (
    code: string,
    workflow: string,
  ) => Promise<Result<LifecycleRecord>>;
  /** Hands the decision back to the binding ladder — `DELETE .../workflow`. */
  readonly inherit: (code: string) => Promise<Result<LifecycleRecord>>;
}

/**
 * Paints a detail across its screen and hands back the two values this module reports.
 *
 * The painter runs for every successful write, including the one that changed the lifecycle without
 * changing the state: risk flags are computed on read (ADR 0011) and the transition bar is rebuilt
 * from `transitions`, so anything less than the whole read leaves one of them stale.
 */
function painted<T extends LifecycleRecord>(
  result: Result<T>,
  paint: (detail: T) => void,
): Result<LifecycleRecord> {
  if (!result.ok) return result;
  paint(result.data);
  return { ok: true, data: result.data };
}

const REASSIGNERS: Readonly<Record<LifecycleScope, Reassigner>> = {
  project: {
    assign: async (code, workflow) =>
      painted<ProjectDetail>(
        await putProjectWorkflow(code, { workflow }),
        applyProjectDetail,
      ),
    inherit: async (code) =>
      painted<ProjectDetail>(
        await deleteProjectWorkflow(code),
        applyProjectDetail,
      ),
  },
  task: {
    assign: async (code, workflow) =>
      painted<TaskDetail>(
        await putTaskWorkflow(code, { workflow }),
        applyTaskDetail,
      ),
    inherit: async (code) =>
      painted<TaskDetail>(await deleteTaskWorkflow(code), applyTaskDetail),
  },
};

/** Which arm of the picker is on screen; one at a time, like every other region on this app. */
type PickerArm = "loading" | "error" | "empty" | "data";

/**
 * Mounts the plate.
 *
 * @param plate - The element carrying `data-lifecycle-plate`.
 * @returns The teardown; drops every listener and closes the dialog.
 */
export function mountLifecyclePlate(plate: HTMLElement): () => void {
  const controller = new AbortController();
  const { signal } = controller;

  revealOpsControls(plate);

  const dialog = plate.querySelector<HTMLDialogElement>(
    "[data-lifecycle-dialog]",
  );
  const form = plate.querySelector<HTMLFormElement>("[data-lifecycle-form]");
  const list = plate.querySelector<HTMLElement>("[data-lifecycle-options]");
  const submit = plate.querySelector<HTMLButtonElement>(
    "[data-action='submit-lifecycle']",
  );

  /** The catalog the open dialog is rendering; the refusal copy names graphs through it. */
  let shapes: readonly WorkflowShape[] = [];

  /**
   * Loads the options and renders whichever arm the answer belongs to.
   *
   * It deliberately leaves any refusal on screen: this also runs *after* a rejected write, to
   * re-read a world that just proved it had moved, and clearing the reason while re-offering the
   * choices would delete the only explanation the operator has.
   */
  const load = async (): Promise<void> => {
    const context = readContext(plate);
    showArm(plate, "loading");
    const result = await getWorkflows();

    if (!result.ok) {
      shapes = [];
      const copy = refusalCopy(result.error, context, []);
      setField(plate, "lifecycle-error-title", "No pudimos leer los flujos");
      setField(
        plate,
        "lifecycle-error-detail",
        `${copy.title}. ${copy.detail}`,
      );
      showArm(plate, "error");
      return;
    }

    shapes = result.data.workflows;
    const options = buildOptions(result.data, context);
    if (options.length === 0) {
      setField(plate, "lifecycle-empty", emptyMessage(context));
      showArm(plate, "empty");
      return;
    }

    renderOptions(plate, list, options);
    showArm(plate, "data");
    syncSubmit(plate, submit);
    focusChosen(list);
  };

  /** Sends the decision, and leaves the plate alone until the server has answered. */
  const send = async (): Promise<void> => {
    if (submit === null) return;
    const context = readContext(plate);
    const choice = chosenChoice(list);
    if (
      choice === null ||
      sameChoice(choice, currentChoice(context.workflow))
    ) {
      return;
    }

    hideRefusal(plate);
    setPending(submit, true);
    setListInert(plate, true);

    const reassigner = REASSIGNERS[context.scope];
    const result =
      choice.kind === "inherit"
        ? await reassigner.inherit(context.code)
        : await reassigner.assign(context.code, choice.code);

    setPending(submit, false);
    setListInert(plate, false);

    if (!result.ok) {
      void shake(submit);
      showRefusal(plate, refusalCopy(result.error, context, shapes));
      // The world moved under the dialog — a colleague retired that graph, or
      // the record was moved — so the options are re-read rather than re-offered.
      void load();
      return;
    }

    dialog?.close();
    announce(context.scope, choice, result.data);
  };

  plate.addEventListener(
    "click",
    (event) => {
      const target = event.target;
      if (!(target instanceof Element)) return;

      if (target.closest("[data-action='open-lifecycle']") !== null) {
        hideRefusal(plate);
        dialog?.showModal();
        void load();
        return;
      }
      if (target.closest("[data-action='close-lifecycle']") !== null) {
        dialog?.close();
        return;
      }
      if (target.closest("[data-action='retry-lifecycle']") !== null) {
        void load();
      }
    },
    { signal },
  );

  // Delegated: the rows are cloned after this listener is bound, so a handler
  // per radio would have to be re-wired on every load.
  list?.addEventListener(
    "change",
    () => {
      hideRefusal(plate);
      syncSubmit(plate, submit);
    },
    { signal },
  );

  form?.addEventListener(
    "submit",
    (event) => {
      // Without this, `<form method="dialog">` closes the dialog on submit and a
      // refusal would have nowhere to be read.
      event.preventDefault();
      void send();
    },
    { signal },
  );

  return () => {
    controller.abort();
    dialog?.close();
  };
}

/**
 * Shows the write controls to an ops lead.
 *
 * Hidden markup rather than absent markup: the page renders before the session is readable, so the
 * gate runs in the browser. Presentation only — the capability is enforced by the API, which answers
 * `403` with `details.required = "ops_lead"` to anyone who unhides the button.
 */
function revealOpsControls(plate: HTMLElement): void {
  const ops = plate.querySelector<HTMLElement>("[data-ops-lead]");
  if (ops === null) return;
  ops.hidden = getOperator()?.isOpsLead !== true;
}

/**
 * The record as the DOM last recorded it.
 *
 * Read from the plate rather than captured at mount, because both facts move underneath it: a
 * transition changes the state the compatibility check compares, and a colleague's reassignment
 * arrives through the painters. A captured copy would be answering with the page's first paint.
 */
function readContext(plate: HTMLElement): LifecycleContext {
  const engagementType = plate.dataset.engagementType ?? "";
  const name = plate.querySelector<HTMLElement>(
    "[data-field='lifecycle-name']",
  )?.textContent;
  return {
    scope: toScope(plate.dataset.scope),
    code: plate.dataset.code ?? "",
    workflow: {
      code: plate.dataset.workflowCode ?? "",
      name: name ?? "",
      source:
        plate.dataset.workflowSource === "DIRECT" ? "DIRECT" : "INHERITED",
    },
    stateCode: plate.dataset.stateCode ?? "",
    stateLabel: plate.dataset.stateLabel ?? "",
    engagementType: engagementType === "" ? null : engagementType,
  };
}

/** Shows exactly one arm of the picker, so two can never be on screen at once. */
function showArm(plate: HTMLElement, arm: PickerArm): void {
  for (const region of plate.querySelectorAll<HTMLElement>(
    "[data-lifecycle-arm]",
  )) {
    region.hidden = region.dataset.lifecycleArm !== arm;
  }
  // Only the `data` arm can produce a decision; the other three leave the
  // primary action visibly dead rather than clickable and pointless.
  const submit = plate.querySelector<HTMLButtonElement>(
    "[data-action='submit-lifecycle']",
  );
  if (submit !== null && arm !== "data") submit.disabled = true;
}

/** Why the picker has nothing to offer: a configuration to make, not a failure to retry. */
function emptyMessage(context: LifecycleContext): string {
  return `${scopeCopy(context.scope).emptyMessage} Créalo en Flujos de trabajo y vuelve aquí.`;
}

/** Rebuilds the rows: this is a set, not a value, so the previous one is dropped whole. */
function renderOptions(
  plate: HTMLElement,
  list: HTMLElement | null,
  options: readonly LifecycleOption[],
): void {
  if (list === null) return;
  list.replaceChildren();

  for (const option of options) {
    const row = cloneTemplate(plate, "lifecycle-option");
    if (row === null) continue;
    const radio = row.querySelector<HTMLInputElement>("[data-lifecycle-radio]");
    if (radio === null) continue;

    radio.dataset.choice = option.choice.kind;
    radio.dataset.workflow = option.code;
    radio.checked = option.isCurrent;
    // An option the target cannot receive is unchoosable rather than choosable
    // and refused. The reason is already on the row, and a disabled radio is
    // skipped by the arrow keys, so the keyboard cannot land on a dead end.
    radio.disabled = option.fit.kind === "missing";

    setField(row, "option-name", option.name);
    setField(row, "option-note", option.note);

    const code = row.querySelector<HTMLElement>("[data-field='option-code']");
    if (code !== null) {
      code.textContent = option.code;
      code.hidden = option.code === "";
    }
    const current = row.querySelector<HTMLElement>("[data-option-current]");
    if (current !== null) current.hidden = !option.isCurrent;

    list.appendChild(row);
  }
}

/** The decision the operator has selected, or `null` while none is. */
function chosenChoice(list: HTMLElement | null): LifecycleChoice | null {
  const radio =
    list?.querySelector<HTMLInputElement>("[data-lifecycle-radio]:checked") ??
    null;
  if (radio === null) return null;
  if (radio.dataset.choice === "inherit") return { kind: "inherit" };
  const code = radio.dataset.workflow ?? "";
  return code === "" ? null : { kind: "workflow", code };
}

/** The submit stays dead until the selection is a *change*; re-choosing the current one is not. */
function syncSubmit(
  plate: HTMLElement,
  submit: HTMLButtonElement | null,
): void {
  if (submit === null) return;
  const list = plate.querySelector<HTMLElement>("[data-lifecycle-options]");
  const choice = chosenChoice(list);
  submit.disabled =
    choice === null ||
    sameChoice(choice, currentChoice(readContext(plate).workflow));
}

/** Puts the keyboard on the decision in force, so the list opens where the operator already is. */
function focusChosen(list: HTMLElement | null): void {
  const radio =
    list?.querySelector<HTMLInputElement>("[data-lifecycle-radio]:checked") ??
    list?.querySelector<HTMLInputElement>(
      "[data-lifecycle-radio]:not(:disabled)",
    );
  radio?.focus();
}

/**
 * Makes the whole set inert while a write is in flight.
 *
 * Through the `<fieldset>` rather than radio by radio: disabling the container leaves each radio's
 * own `disabled` untouched, so the incompatible ones stay incompatible when it is lifted instead of
 * being handed back as choosable.
 */
function setListInert(plate: HTMLElement, inert: boolean): void {
  const fieldset = plate.querySelector<HTMLFieldSetElement>(
    "fieldset[data-lifecycle-arm='data']",
  );
  if (fieldset !== null) fieldset.disabled = inert;
}

/** Shows the refusal where the operator is looking, and keeps the dialog open. */
function showRefusal(plate: HTMLElement, copy: RefusalCopy): void {
  const box = plate.querySelector<HTMLElement>("[data-lifecycle-refusal]");
  if (box === null) return;
  setField(box, "refusal-title", copy.title);
  setField(box, "refusal-detail", copy.detail);
  box.hidden = false;
  // Announced only once it has content: a live region that becomes visible while
  // empty reads to a screen reader as an alert with nothing in it.
  box.setAttribute("role", "alert");
}

/** Clears the refusal; a new choice deserves a clean slate. */
function hideRefusal(plate: HTMLElement): void {
  const box = plate.querySelector<HTMLElement>("[data-lifecycle-refusal]");
  if (box === null) return;
  box.hidden = true;
  box.removeAttribute("role");
}

/** Names what happened, from the server's answer rather than from what was asked for. */
function announce(
  scope: LifecycleScope,
  choice: LifecycleChoice,
  record: LifecycleRecord,
): void {
  const copy = scopeCopy(scope);
  if (choice.kind === "inherit") {
    toast({
      kind: "success",
      title: "Vuelve a heredar su flujo",
      detail: `${copy.subjectStart} sigue «${record.workflow.name}» porque se lo da su tipo de encargo, no porque nadie se lo haya impuesto. Sigue en «${record.state.label}».`,
    });
    return;
  }
  toast({
    kind: "success",
    title: "Flujo actualizado",
    detail: `«${record.workflow.name}» manda ahora sobre ${copy.subject}. Sigue en «${record.state.label}», con los movimientos de su nuevo flujo.`,
  });
}
