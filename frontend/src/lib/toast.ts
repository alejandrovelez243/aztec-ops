/**
 * Toasts — the "result" arm of the feedback inventory (DESIGN.md §Motion).
 *
 * One fixed region (`#toast-region`, rendered by the shell with
 * `aria-live="polite"`), stacked cards springing in on `carta`. Success and
 * info dismiss themselves; errors stay until closed, because a failure the
 * operator never saw is a silent failure — the thing the product principle
 * forbids.
 *
 * Invariant: this module owns every node inside the region. Nothing else
 * appends there, so eviction (max {@link MAX_TOASTS}) can assume every child
 * is a toast it created.
 */

import { prefersReducedMotion, sampleSpring } from "./motion/spring";

/** Semantic kind of one toast; maps onto the tone classes of base.css. */
export type ToastKind = "success" | "error" | "info";

/**
 * A single recovery offered beside the notice — "Deshacer" after a removal.
 *
 * One, never a row of them: a toast is an aside that dismisses itself, so a
 * second choice inside it is a decision the operator can lose by waiting. Any
 * act that deserves more than one option deserves a surface that stays.
 */
export interface ToastAction {
  /** The button's words, in the operator's language. */
  readonly label: string;
  /**
   * What the press does. Fired at most once — the toast leaves on the press —
   * so an implementation need not guard against a double invocation. It must
   * report its own outcome (a second toast, a repaint); this module only
   * dismisses.
   */
  readonly onSelect: () => void;
}

/** One toast request. */
export interface ToastOptions {
  readonly kind: ToastKind;
  /** One line naming what happened, in the product's own words. */
  readonly title: string;
  /** Optional second line: the reason or the recovery. */
  readonly detail?: string;
  /**
   * Optional recovery, rendered as a button inside the toast.
   *
   * Note the deadline this inherits: on a `success` or `info` toast the offer
   * disappears with the toast after {@link DISMISS_AFTER_MS}. Attach it only
   * where letting it lapse is an acceptable answer.
   */
  readonly action?: ToastAction;
}

/** Auto-dismiss delay per kind; `null` means it stays until closed. */
const DISMISS_AFTER_MS: Record<ToastKind, number | null> = {
  success: 4_500,
  info: 4_500,
  error: null,
};

const TONE_CLASS: Record<ToastKind, string> = {
  success: "tone-verde",
  error: "tone-rojo",
  info: "tone-cielo",
};

/** Oldest toasts are evicted beyond this many on screen. */
const MAX_TOASTS = 4;

/**
 * Shows one toast. Safe to call before the region exists (SSR, tests): the
 * call is then dropped rather than throwing mid-action.
 */
export function toast(options: ToastOptions): void {
  if (typeof document === "undefined") return;
  const region = document.getElementById("toast-region");
  if (region === null) return;

  while (region.children.length >= MAX_TOASTS) {
    region.firstElementChild?.remove();
  }

  const el = document.createElement("div");
  el.className = `toast ${TONE_CLASS[options.kind]}`;
  if (options.kind === "error") el.setAttribute("role", "alert");

  const dot = document.createElement("span");
  dot.className = "dot";
  el.appendChild(dot);

  const textWrap = document.createElement("div");
  // Claims the free space, so the optional action and the close button sit
  // together at the trailing edge instead of drifting apart.
  textWrap.className = "toast-text";
  const title = document.createElement("p");
  title.className = "toast-title";
  title.textContent = options.title;
  textWrap.appendChild(title);
  if (options.detail !== undefined && options.detail !== "") {
    const detail = document.createElement("p");
    detail.className = "toast-detail";
    detail.textContent = options.detail;
    textWrap.appendChild(detail);
  }
  el.appendChild(textWrap);

  const action = options.action;
  if (action !== undefined) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "toast-action";
    button.textContent = action.label;
    button.addEventListener(
      "click",
      () => {
        // Dismissed first: the recovery paints its own answer, and leaving the
        // notice of the act that was just undone on screen contradicts it.
        dismiss(el);
        action.onSelect();
      },
      { once: true },
    );
    el.appendChild(button);
  }

  const close = document.createElement("button");
  close.type = "button";
  close.className = "toast-close";
  close.setAttribute("aria-label", "Cerrar aviso");
  close.textContent = "✕";
  close.addEventListener("click", () => dismiss(el));
  el.appendChild(close);

  region.appendChild(el);
  enter(el);

  const after = DISMISS_AFTER_MS[options.kind];
  if (after !== null) {
    window.setTimeout(() => dismiss(el), after);
  }
}

function enter(el: HTMLElement): void {
  if (prefersReducedMotion()) return;
  const { duration, easing } = sampleSpring("carta");
  el.animate(
    [
      { opacity: 0, transform: "translateY(12px) scale(0.98)" },
      { opacity: 1, transform: "none" },
    ],
    { duration, easing },
  );
}

function dismiss(el: HTMLElement): void {
  if (!el.isConnected) return;
  if (prefersReducedMotion()) {
    el.remove();
    return;
  }
  const exit = el.animate(
    [
      { opacity: 1, transform: "none" },
      { opacity: 0, transform: "translateY(8px) scale(0.98)" },
    ],
    { duration: 180, easing: "ease-out" },
  );
  exit.finished
    .catch(() => undefined)
    .finally(() => {
      el.remove();
    });
}
