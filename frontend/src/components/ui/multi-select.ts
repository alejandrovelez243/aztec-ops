/**
 * The behaviour half of `MultiSelect.astro`: chips in, chips out, and nothing
 * offered twice.
 *
 * The panel is not this module's business — `lib/ui/menu.ts` owns opening,
 * closing, `Escape`, the arrow keys and the one-open-panel rule, exactly as it
 * does for `MenuSelect`, `OwnerMenu` and `DateField`. What lives here is the
 * plural part: a chosen option leaves the panel (`disabled` *and* `hidden`, so
 * the arrow keys cannot land on something already chosen) and comes back the
 * moment its chip is removed.
 *
 * **The chosen set is the DOM.** There is no hidden input and no module-level
 * state: the chips *are* the value, which is why {@link multiValues} can be read
 * at submit time and why a repaint can never disagree with what the operator is
 * looking at.
 *
 * **Focus is never dropped.** Removing a chip removes the button that was
 * focused, and a browser hands focus to `<body>` when that happens — a keyboard
 * operator would be thrown to the top of the dialog mid-answer. Removal
 * therefore moves focus deliberately, to the next chip or back to the trigger.
 */

import { cloneTemplate, setField } from "../../lib/dom/patch";
import { mountMenus } from "../../lib/ui/menu";
import type { MultiOption } from "./MultiSelect.astro";

export type { MultiOption };

const SELECTOR = {
  control: "[data-multi-select]",
  chips: "[data-multi-chips]",
  chip: "[data-multi-chip]",
  remove: "[data-multi-remove]",
  empty: "[data-multi-empty]",
  exhausted: "[data-multi-exhausted]",
  panel: "[data-menu-panel]",
  trigger: "[data-menu-trigger]",
  item: "[data-menu-item]",
} as const;

/**
 * Wires every `[data-multi-select]` inside `container`.
 *
 * @param container - Delegation root; must outlive the controls inside it.
 * @returns The teardown; drops the listeners and closes any open panel.
 */
export function mountMultiSelects(container: HTMLElement): () => void {
  const controller = new AbortController();
  const { signal } = controller;

  const releaseMenus = mountMenus(container, SELECTOR.control, {
    // Server-rendered, or re-set by the surface through `setMultiOptions`
    // before the dialog opens: the panel is already correct when it opens.
    fill: () => Promise.resolve(),
    choose: (control, item) => {
      choose(control, item);
      return Promise.resolve();
    },
  });

  container.addEventListener(
    "click",
    (event) => {
      const target = event.target;
      if (!(target instanceof Element)) return;
      const button = target.closest<HTMLElement>(SELECTOR.remove);
      if (button === null) return;
      const control = button.closest<HTMLElement>(SELECTOR.control);
      const chip = button.closest<HTMLElement>(SELECTOR.chip);
      if (control === null || chip === null) return;
      remove(control, chip);
    },
    { signal },
  );

  return () => {
    controller.abort();
    releaseMenus();
  };
}

/**
 * The control carrying `facet` inside `root`, or `null` when it is not there.
 *
 * `null` rather than a throw: an island script is evaluated on every navigation,
 * and a page that does not host the control must produce a no-op.
 */
export function findMultiSelect(
  root: ParentNode,
  facet: string,
): HTMLElement | null {
  return root.querySelector<HTMLElement>(
    `${SELECTOR.control}[data-facet="${CSS.escape(facet)}"]`,
  );
}

/** The chosen values, in the order the operator added them. */
export function multiValues(control: HTMLElement): readonly string[] {
  return [...control.querySelectorAll<HTMLElement>(SELECTOR.chip)].flatMap(
    (chip) => {
      const value = chip.dataset["value"] ?? "";
      return value === "" ? [] : [value];
    },
  );
}

/**
 * Replaces what the control offers, and drops every chip.
 *
 * For the surfaces that only learn their list at runtime — the board knows which
 * project it is creating a task on only once one is selected, so its options are
 * the tasks currently painted on it. Keeping the old chips would let a
 * dependency from the *previous* project ride along into this POST, which the
 * server would accept as an unresolvable `raw_label` and nobody would notice.
 */
export function setMultiOptions(
  control: HTMLElement,
  options: readonly MultiOption[],
): void {
  const panel = control.querySelector<HTMLElement>(SELECTOR.panel);
  if (panel === null) return;
  clearMultiSelect(control);
  panel.replaceChildren();
  for (const option of options) {
    const item = cloneTemplate(control, "multi-option");
    if (item === null) continue;
    item.dataset["value"] = option.value;
    setField(item, "option-code", option.value);
    setField(item, "option-label", option.label);
    panel.appendChild(item);
  }
  sync(control);
}

/** Removes every chip and re-offers every option. */
export function clearMultiSelect(control: HTMLElement): void {
  const chips = control.querySelector<HTMLElement>(SELECTOR.chips);
  if (chips !== null) chips.replaceChildren();
  for (const item of control.querySelectorAll<HTMLButtonElement>(
    SELECTOR.item,
  )) {
    item.disabled = false;
    item.hidden = false;
  }
  sync(control);
}

/** Turns one offered option into a chip. */
function choose(control: HTMLElement, item: HTMLElement): void {
  const value = item.dataset["value"] ?? "";
  if (value === "") return;
  const chips = control.querySelector<HTMLElement>(SELECTOR.chips);
  if (chips === null) return;

  const chip = cloneTemplate(control, "multi-chip");
  if (chip === null) return;
  chip.dataset["value"] = value;
  setField(chip, "chip-code", value);
  // The code, spoken as part of the remove button's name, so "Quitar" is never
  // read out on its own beside four identical siblings.
  setField(chip, "chip-remove-code", value);
  chips.appendChild(chip);

  if (item instanceof HTMLButtonElement) item.disabled = true;
  item.hidden = true;
  sync(control);
}

/** Takes one chip back off, and re-offers what it named. */
function remove(control: HTMLElement, chip: HTMLElement): void {
  const value = chip.dataset["value"] ?? "";
  const next = chip.nextElementSibling;
  chip.remove();

  for (const item of control.querySelectorAll<HTMLButtonElement>(
    SELECTOR.item,
  )) {
    if ((item.dataset["value"] ?? "") !== value) continue;
    item.disabled = false;
    item.hidden = false;
  }
  sync(control);

  const following =
    next instanceof HTMLElement
      ? next.querySelector<HTMLButtonElement>(SELECTOR.remove)
      : null;
  if (following !== null) {
    following.focus();
    return;
  }
  const trigger = control.querySelector<HTMLButtonElement>(SELECTOR.trigger);
  if (trigger !== null && !trigger.disabled) trigger.focus();
}

/**
 * Brings the three dependent parts back in line: the chip list, the "nothing
 * chosen" line, and whether there is anything left to add.
 *
 * With nothing left to offer the trigger goes dead with the reason named beside
 * it, rather than opening onto an empty panel — a control that opens to nothing
 * reads as a broken screen (`FRONTEND.md` §10).
 */
function sync(control: HTMLElement): void {
  const chips = control.querySelector<HTMLElement>(SELECTOR.chips);
  const empty = control.querySelector<HTMLElement>(SELECTOR.empty);
  const exhausted = control.querySelector<HTMLElement>(SELECTOR.exhausted);
  const trigger = control.querySelector<HTMLButtonElement>(SELECTOR.trigger);

  const chosen = control.querySelectorAll(SELECTOR.chip).length;
  if (chips !== null) chips.hidden = chosen === 0;
  if (empty !== null) empty.hidden = chosen > 0;

  const available = [
    ...control.querySelectorAll<HTMLButtonElement>(SELECTOR.item),
  ].filter((item) => !item.disabled).length;
  if (exhausted !== null) exhausted.hidden = available > 0;
  if (trigger === null) return;
  trigger.disabled = available === 0;
  if (available === 0 && exhausted !== null && exhausted.id !== "") {
    trigger.setAttribute("aria-describedby", exhausted.id);
    return;
  }
  trigger.removeAttribute("aria-describedby");
}
