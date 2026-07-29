/**
 * The one place this app asks before destroying something.
 *
 * Promise-shaped rather than callback-shaped because that is what the callers
 * are: a removal is `if (!(await confirmDestructive({...}))) return;` followed by
 * the DELETE. Every non-answer — Cancelar, `Escape`, a click on the backdrop, a
 * second ask superseding this one, or a page that renders no dialog — resolves
 * `false`, so the failure mode of this module is always "nothing happened".
 *
 * It deliberately does not perform the act, name the endpoint or paint the
 * result. It asks a question and reports the answer; a confirmation that also
 * knew how to delete a task could not be reused by the next thing that needs one.
 */

import { setField } from "../../lib/dom/patch";

/** The words one confirmation puts on screen. */
export interface ConfirmRequest {
  /** The heading; names the specific act, never "¿Confirmar?". */
  readonly title: string;
  /**
   * The paragraph under it. For a destructive act this is where the mechanical
   * truth goes — above all, what *survives* the act, which is the operator's
   * real question.
   */
  readonly body: string;
  /** The destructive button's words; the verb, not "Aceptar". */
  readonly confirmLabel: string;
}

/** Settles the currently open ask, if any. */
let settleOpen: ((confirmed: boolean) => void) | null = null;

/**
 * Opens the confirmation and resolves with the operator's answer.
 *
 * Initial focus lands on **Cancelar**, not on the destructive button: the
 * dialog exists to interrupt momentum, and a modal that puts the irreversible
 * act under the `Enter` key the operator was already pressing interrupts
 * nothing.
 *
 * @param request - The copy for this one ask.
 * @returns `true` only when the destructive button was pressed. Anything else,
 *   including a page hosting no `[data-confirm-dialog]`, is `false`.
 */
export function confirmDestructive(request: ConfirmRequest): Promise<boolean> {
  const dialog = document.querySelector<HTMLDialogElement>(
    "[data-confirm-dialog]",
  );
  if (dialog === null) return Promise.resolve(false);
  const cancel = dialog.querySelector<HTMLButtonElement>(
    "[data-action='cancel-confirm']",
  );
  if (cancel === null) return Promise.resolve(false);

  // A second ask supersedes the first rather than queueing behind it: there is
  // one dialog element, so two open asks would share one set of words.
  settleOpen?.(false);

  setField(dialog, "confirm-title", request.title);
  setField(dialog, "confirm-body", request.body);
  setField(dialog, "confirm-accept", request.confirmLabel);

  return new Promise<boolean>((resolve) => {
    const controller = new AbortController();
    const { signal } = controller;

    const finish = (confirmed: boolean): void => {
      // Listeners go first: `close()` below fires the `close` event, and a
      // still-attached handler would settle this promise a second time.
      controller.abort();
      settleOpen = null;
      if (dialog.open) dialog.close();
      resolve(confirmed);
    };
    settleOpen = finish;

    dialog.addEventListener(
      "click",
      (event) => {
        const target = event.target;
        if (!(target instanceof Element)) return;
        if (target.closest("[data-action='accept-confirm']") !== null) {
          finish(true);
          return;
        }
        if (target.closest("[data-action='cancel-confirm']") !== null) {
          finish(false);
        }
      },
      { signal },
    );

    // Covers `Escape` and any other native dismissal.
    dialog.addEventListener("close", () => finish(false), { signal });

    dialog.showModal();
    cancel.focus();
  });
}
