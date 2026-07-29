/**
 * The one place this screen asks "¿qué está detenido?".
 *
 * Promise-shaped rather than callback-shaped because that is what the caller
 * is: raising a blocker is `const draft = await askBlocker(root)` followed by
 * the POST, and cancelling is a `null` that ends the gesture instead of an
 * abandoned request the operator never sees.
 *
 * The dialog is not settled twice and never left half-open: one module-level
 * settle so a second ask supersedes the first, one `AbortController` per open,
 * and the listeners dropped *before* `close()` — the `close` event fires from
 * inside `finish`, and a still-attached handler would resolve the promise a
 * second time with `null` after it had already resolved with a draft.
 *
 * Validation lives here, in the field, because the dialog is where the operator
 * can still fix it: a description that is only whitespace or a kind the wire
 * union does not contain closes nothing and posts nothing.
 */

import { mountMenus } from "../../lib/ui/menu";
import { setField } from "./dom";
import { BLOCKER_KINDS } from "./messages";
import type { BlockerKind } from "../../lib/api/domain";

/** What an incomplete submission is answered with, in the dialog itself. */
const INCOMPLETE = "Elige el tipo y describe qué está detenido.";

/** A blocker the operator has finished describing, ready to be posted. */
export interface BlockerDraft {
  readonly kind: BlockerKind;
  readonly description: string;
}

/** Settles the currently open ask, if any. */
let settleOpen: ((draft: BlockerDraft | null) => void) | null = null;

/**
 * Opens the raise-blocker dialog and resolves with what the operator described.
 *
 * @param root - The blockers panel; the dialog is rendered inside it.
 * @returns The chosen kind and the trimmed description, or `null` when
 *   cancelled, dismissed with `Escape`, superseded by another ask, or when the
 *   panel hosts no dialog — every one of which means "no registres nada",
 *   never "regístralo igual".
 */
export function askBlocker(root: HTMLElement): Promise<BlockerDraft | null> {
  const dialog = root.querySelector<HTMLDialogElement>("[data-raise-dialog]");
  if (dialog === null) return Promise.resolve(null);
  const form = dialog.querySelector<HTMLFormElement>("[data-raise-form]");
  if (form === null) return Promise.resolve(null);

  settleOpen?.(null);

  const errorLine = dialog.querySelector<HTMLElement>(
    "[data-field='raise-error']",
  );
  form.reset();
  if (errorLine !== null) errorLine.hidden = true;
  // `form.reset()` cannot reach the kind picker: `MenuSelect` is a button and a
  // panel, not a form control, so a second ask would otherwise open on the kind
  // the previous one chose.
  resetKind(dialog);

  return new Promise<BlockerDraft | null>((resolve) => {
    const controller = new AbortController();
    const { signal } = controller;
    // The popup grammar is `lib/ui/menu.ts`'s, mounted for as long as the ask
    // lasts. Its panel is `position: fixed`, which inside a top-layer `<dialog>`
    // still anchors to the viewport, so no extra stacking work is needed here.
    const unmountMenus = mountMenus(dialog, "[data-menu-select]", {
      fill: () => Promise.resolve(),
      choose: (control, item) => {
        paintChoice(control, item);
        return Promise.resolve();
      },
    });

    const finish = (draft: BlockerDraft | null): void => {
      // Listeners go first: `close()` below fires the `close` event, and a
      // still-attached handler would settle this promise a second time.
      controller.abort();
      unmountMenus();
      settleOpen = null;
      if (dialog.open) dialog.close();
      resolve(draft);
    };
    settleOpen = finish;

    form.addEventListener(
      "submit",
      (event) => {
        event.preventDefault();
        const description = String(
          new FormData(form).get("description") ?? "",
        ).trim();
        const kind = chosenKind(dialog);
        if (description === "" || kind === null) {
          if (errorLine !== null) {
            errorLine.hidden = false;
            errorLine.textContent = INCOMPLETE;
          }
          return;
        }
        if (errorLine !== null) errorLine.hidden = true;
        finish({ kind, description });
      },
      { signal },
    );

    dialog.addEventListener(
      "click",
      (event) => {
        const target = event.target;
        if (!(target instanceof Element)) return;
        if (target.closest("[data-action='close-raise']") !== null) {
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

/** The kind control, or `null` on a page that does not render one. */
function kindControl(dialog: HTMLElement): HTMLElement | null {
  return dialog.querySelector<HTMLElement>(
    "[data-menu-select][data-facet='blocker-kind']",
  );
}

/**
 * The chosen kind, narrowed onto the wire union without asserting it.
 *
 * `BLOCKER_KINDS` is the vocabulary the picker was rendered from, so a value
 * that is not in it can only come from tampered markup; it reads as "sin
 * elegir" and the submission is refused here rather than sent for the server
 * to reject with a 422.
 */
function chosenKind(dialog: HTMLElement): BlockerKind | null {
  const value = kindControl(dialog)?.dataset["value"] ?? "";
  return BLOCKER_KINDS.find((kind) => kind === value) ?? null;
}

/** Returns the picker to the first kind the vocabulary offers. */
function resetKind(dialog: HTMLElement): void {
  const control = kindControl(dialog);
  if (control === null) return;
  const first = control.querySelector<HTMLElement>("[data-menu-item]");
  if (first === null) return;
  paintChoice(control, first);
}

/** Writes a choice onto the control: its value, its ticks and its trigger. */
function paintChoice(control: HTMLElement, item: HTMLElement): void {
  const value = item.dataset["value"] ?? "";
  control.dataset["value"] = value;
  setField(control, "menu-current", item.textContent?.trim() ?? "");

  for (const option of control.querySelectorAll<HTMLElement>(
    "[data-menu-item]",
  )) {
    const isCurrent = (option.dataset["value"] ?? "") === value;
    option.setAttribute("aria-checked", String(isCurrent));
    const tick = option.querySelector<HTMLElement>("[data-option-tick]");
    if (tick !== null) tick.hidden = !isCurrent;
  }
}
