/**
 * The one place this screen asks "¿por qué?".
 *
 * `requires_reason` is a property of a `WorkflowTransition` row, not of the
 * project workflow, so a task's move can demand a motive exactly as a project's
 * can. Both therefore ask with the same words, the same validation and the same
 * dialog element — a second copy of this form would be a second chance to
 * disagree about what "el motivo queda en la bitácora" means.
 *
 * Promise-shaped rather than callback-shaped because that is what the callers
 * are: a transition is `const reason = await askReason(label)` followed by the
 * POST, and cancelling is a `null` that ends the gesture instead of an
 * abandoned request the operator never sees.
 */

import { setField } from "./dom";

/** What an empty submission is answered with, in the field itself. */
const EMPTY_REASON = "Esta transición no se puede registrar sin un motivo.";

/** Settles the currently open ask, if any. */
let settleOpen: ((reason: string | null) => void) | null = null;

/**
 * Opens the reason dialog and resolves with what the operator wrote.
 *
 * @param targetLabel - The destination state, named in the dialog's title so the
 *   operator is confirming a specific move rather than "un cambio".
 * @returns The trimmed motive, or `null` when cancelled, dismissed with
 *   `Escape`, superseded by another ask, or when the page hosts no dialog —
 *   every one of which means "no sigas", never "sigue sin motivo".
 */
export function askReason(targetLabel: string): Promise<string | null> {
  const dialog = document.querySelector<HTMLDialogElement>(
    "[data-reason-dialog]",
  );
  if (dialog === null) return Promise.resolve(null);
  const form = dialog.querySelector<HTMLFormElement>("[data-reason-form]");
  if (form === null) return Promise.resolve(null);

  settleOpen?.(null);

  const errorLine = dialog.querySelector<HTMLElement>(
    "[data-field='reason-error']",
  );
  form.reset();
  if (errorLine !== null) errorLine.hidden = true;
  setField(dialog, "reason-target", targetLabel);

  return new Promise<string | null>((resolve) => {
    const controller = new AbortController();
    const { signal } = controller;

    const finish = (reason: string | null): void => {
      // Listeners go first: `close()` below fires the `close` event, and an
      // still-attached handler would settle this promise a second time.
      controller.abort();
      settleOpen = null;
      if (dialog.open) dialog.close();
      resolve(reason);
    };
    settleOpen = finish;

    form.addEventListener(
      "submit",
      (event) => {
        event.preventDefault();
        const reason = String(new FormData(form).get("reason") ?? "").trim();
        if (reason === "") {
          if (errorLine !== null) {
            errorLine.hidden = false;
            errorLine.textContent = EMPTY_REASON;
          }
          return;
        }
        finish(reason);
      },
      { signal },
    );

    dialog.addEventListener(
      "click",
      (event) => {
        const target = event.target;
        if (!(target instanceof Element)) return;
        if (target.closest("[data-action='close-reason']") !== null) {
          finish(null);
        }
      },
      { signal },
    );

    // Covers `Escape` and any other native dismissal.
    dialog.addEventListener("close", () => finish(null), { signal });

    dialog.showModal();
  });
}
