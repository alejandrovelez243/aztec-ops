/**
 * The two edits the header owns: the próximo paso, and who is responsible.
 *
 * Both are `PATCH /api/v1/projects/{code}` and both end the same way — the
 * response is the project **re-read after the write**, so the risk flags
 * computed on read (ADR 0011) already reflect the edit. That is the whole point
 * of painting from it: the operator writes the próximo paso and watches "Sin
 * próximo paso" leave the risk list in the same gesture, because the answer
 * they are being shown is the answer to "¿sigue faltando?".
 *
 * Nothing is painted before that answer arrives. A `PATCH` can be refused —
 * an owner code that matches nobody, a next step past its length — and a field
 * repainted ahead of the server would have to be rolled back under the reader's
 * eyes. The save button holds the pending state instead
 * (`docs/standards/PATTERNS_FRONTEND.md` §8).
 */

import { patchProject } from "../../lib/api/client";
import { shake } from "../../lib/motion/spring";
import { toast } from "../../lib/toast";
import { setPending } from "./dom";
import { failureCopy } from "./messages";
import { mountOwnerMenus } from "./owner-menu";
import { applyProjectDetail, hasRisk } from "./project-paint";
import { invalidateRoster } from "../../lib/team/roster";

/**
 * The risk this field exists to answer. A flag code, never a label: codes are
 * structural (one class plus one registry entry, CLAUDE.md rule 8) while the
 * label is Spanish prose the specification owns.
 */
const NO_NEXT_STEP = "NO_NEXT_STEP";

/** What the operator is told when the edit lands and the flag is gone with it. */
const RISK_CLEARED = "El riesgo «Sin próximo paso» quedó resuelto.";

/**
 * Mounts the header's writes.
 *
 * @param header - The element carrying `data-project-header`.
 * @returns The teardown; drops every listener and closes any open menu.
 */
export function mountHeaderControls(header: HTMLElement): () => void {
  const controller = new AbortController();
  const { signal } = controller;

  const field = header.querySelector<HTMLElement>("[data-next-step-field]");
  const form = header.querySelector<HTMLFormElement>("[data-next-step-form]");
  const input = header.querySelector<HTMLInputElement>(
    "[data-next-step-input]",
  );
  const code = field?.dataset.projectCode ?? "";

  /** Shows the editor, primed with what is on screen. */
  const startEditing = (): void => {
    if (field === null || input === null) return;
    field.dataset.mode = "edit";
    input.focus();
    input.select();
  };

  /** Returns to the read arm, discarding the draft. */
  const stopEditing = (): void => {
    if (field === null || input === null) return;
    field.dataset.mode = "view";
    // The truth is whatever the last authoritative read wrote into the display
    // node; the draft is dropped rather than kept for the next open.
    input.value =
      field.querySelector<HTMLElement>("[data-field='next-step-value']")
        ?.textContent ?? "";
    hideError();
    // Two affordances open the editor — the "Editar" button and the invitation
    // — and `data-filled` hides one of them. Focusing the hidden one is a
    // silent no-op that would leave the keyboard at the top of the document.
    for (const opener of field.querySelectorAll<HTMLButtonElement>(
      "[data-action='edit-next-step']",
    )) {
      if (opener.offsetParent === null) continue;
      opener.focus();
      break;
    }
  };

  const errorLine = header.querySelector<HTMLElement>(
    "[data-field='next-step-error']",
  );

  const hideError = (): void => {
    if (errorLine !== null) errorLine.hidden = true;
  };

  const showError = (message: string): void => {
    if (errorLine === null) return;
    errorLine.hidden = false;
    errorLine.textContent = message;
  };

  header.addEventListener(
    "click",
    (event) => {
      const target = event.target;
      if (!(target instanceof Element)) return;
      if (target.closest("[data-action='edit-next-step']") !== null) {
        startEditing();
        return;
      }
      if (target.closest("[data-action='cancel-next-step']") !== null) {
        stopEditing();
      }
    },
    { signal },
  );

  // Escape cancels from inside the input; Enter is the form's own submit, so it
  // needs no key handling and keeps working for assistive technology.
  input?.addEventListener(
    "keydown",
    (event) => {
      if (event.key !== "Escape") return;
      event.preventDefault();
      stopEditing();
    },
    { signal },
  );

  form?.addEventListener(
    "submit",
    (event) => {
      event.preventDefault();
      void saveNextStep();
    },
    { signal },
  );

  async function saveNextStep(): Promise<void> {
    if (form === null || input === null || field === null) return;
    const button = form.querySelector<HTMLButtonElement>(
      "[data-action='save-next-step']",
    );
    if (button === null) return;

    const value = input.value.trim();
    hideError();
    setPending(button, true);
    // An emptied field is an explicit `null`: "ya no hay próximo paso" is a
    // decision, and the API distinguishes it from "no lo toques" by the key
    // being present. Sending "" would store an empty string that reads as
    // written-but-blank and would not raise the risk again.
    const result = await patchProject(code, {
      next_step: value === "" ? null : value,
    });
    setPending(button, false);

    if (!result.ok) {
      void shake(button);
      const copy = failureCopy(result.error);
      showError(`${copy.title}. ${copy.detail}`);
      toast({ kind: "error", title: copy.title, detail: copy.detail });
      return;
    }

    field.dataset.mode = "view";
    applyProjectDetail(result.data);
    const cured = value !== "" && !hasRisk(result.data, NO_NEXT_STEP);
    toast({
      kind: "success",
      title: value === "" ? "Próximo paso borrado" : "Próximo paso guardado",
      // `exactOptionalPropertyTypes`: an absent detail is absent, never `undefined`.
      ...(cured ? { detail: RISK_CLEARED } : {}),
    });
  }

  const offOwner = mountOwnerMenus(header, async (_control, alias) => {
    const result = await patchProject(code, { owner: alias });
    if (!result.ok) {
      const copy = failureCopy(result.error);
      toast({ kind: "error", title: copy.title, detail: copy.detail });
      return;
    }
    applyProjectDetail(result.data);
    // Two people's load just changed, and the picker shows it beside their name.
    invalidateRoster();
    toast({
      kind: "success",
      title: "Responsable actualizado",
      detail:
        result.data.owner === null || result.data.owner === undefined
          ? "El proyecto quedó sin responsable."
          : `Ahora responde ${result.data.owner.label}.`,
    });
  });

  return () => {
    controller.abort();
    offOwner();
  };
}
