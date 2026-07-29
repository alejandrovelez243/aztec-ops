/**
 * Reading and writing a {@link GraphPicker} from the editor.
 *
 * The control has no hidden input: `data-value` on the wrapper *is* the field, and the trigger's
 * word and the ticks are what the operator reads back. Everything here moves all three together,
 * so a chosen value can never disagree with the label beside it — the same contract
 * `create-task.ts` keeps with `MenuSelect`.
 *
 * Framework-free and DOM-only: it knows what a picker looks like, never what a choice means.
 */
import { cloneTemplate, setField } from "../../lib/dom/patch";
import type { Choice } from "./authoring";

/** The picker with this facet inside `scope`, or `null` when the surface has none. */
export function findPicker(
  scope: ParentNode,
  facet: string,
): HTMLElement | null {
  return scope.querySelector<HTMLElement>(
    `[data-graph-picker][data-facet="${CSS.escape(facet)}"]`,
  );
}

/** What the picker currently holds; `""` when nothing is chosen. */
export function pickerValue(control: HTMLElement): string {
  return control.dataset["value"] ?? "";
}

/**
 * Paints a choice, which is also how the control stores it.
 *
 * A `value` no option carries clears the control back to its placeholder rather than leaving the
 * previous word on screen: a trigger reading "En progreso" over a `data-value` of `""` is the
 * one failure a picker must not have.
 */
export function selectPicker(control: HTMLElement, value: string): void {
  const items = [...control.querySelectorAll<HTMLElement>("[data-menu-item]")];
  const chosen = items.find((item) => (item.dataset["value"] ?? "") === value);

  control.dataset["value"] = chosen === undefined ? "" : value;
  setField(
    control,
    "picker-current",
    chosen === undefined
      ? (control.dataset["placeholder"] ?? "")
      : (
          chosen.querySelector("[data-field='option-label']")?.textContent ?? ""
        ).trim(),
  );

  for (const item of items) {
    const isChosen = item === chosen;
    item.setAttribute("aria-checked", String(isChosen));
    const tick = item.querySelector<HTMLElement>("[data-option-tick]");
    if (tick !== null) tick.hidden = !isChosen;
  }
}

/**
 * Replaces the picker's options, then re-selects `value`.
 *
 * Called on every open for the graph-shaped pickers: which states a move may join changes while
 * the operator works, and an option list built once would offer a state that has since been
 * retired. Options are cloned from the control's own `<template>` so they keep the component's
 * scope attribute and therefore its styles.
 */
export function setPickerOptions(
  control: HTMLElement,
  options: readonly Choice[],
  value: string,
): void {
  const panel = control.querySelector<HTMLElement>("[data-menu-panel]");
  if (panel === null) return;

  const fragment = document.createDocumentFragment();
  for (const option of options) {
    const item = cloneTemplate(control, "picker-option");
    if (item === null) continue;
    item.dataset["value"] = option.value;
    setField(item, "option-label", option.label);
    fragment.append(item);
  }
  panel.replaceChildren(fragment);
  selectPicker(control, value);
}
