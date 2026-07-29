/**
 * Reading and writing a `MenuSelect` from the island that mounts it.
 *
 * The control has **no hidden input**: `data-value` on the `[data-menu-select]`
 * wrapper *is* the field, and the trigger's word plus the ticks are what the
 * operator reads back. Three places therefore have to move together on every
 * choice, and an island that moved two of them would show a value that
 * disagrees with the label beside it — which is why this is written once here
 * rather than per surface.
 *
 * It lives in `lib/` for the same reason `lib/ui/menu.ts` does: none of it knows
 * what a project, a role or a score is. Open, close, `Escape` and the arrow keys
 * are still {@link mountMenus}'s; this module only touches the value.
 */

/** One choosable value, mirroring `MenuSelect`'s own `MenuOption`. */
export interface MenuOption {
  readonly value: string;
  readonly label: string;
}

/** The `[data-menu-select]` carrying `facet`, or `null` when none is rendered. */
export function findMenuSelect(
  root: ParentNode,
  facet: string,
): HTMLElement | null {
  return root.querySelector<HTMLElement>(
    `[data-menu-select][data-facet="${CSS.escape(facet)}"]`,
  );
}

/** The value `facet` currently holds, `""` when it holds none or is absent. */
export function menuSelectValue(root: ParentNode, facet: string): string {
  return findMenuSelect(root, facet)?.dataset["value"] ?? "";
}

/**
 * Writes a choice onto a control: its `data-value`, its trigger and its ticks.
 *
 * @param control - The `[data-menu-select]` wrapper.
 * @param item - The `[data-menu-item]` that was chosen; must belong to
 *   `control`, otherwise the trigger would announce another control's word.
 */
export function applyMenuChoice(control: HTMLElement, item: HTMLElement): void {
  const value = item.dataset["value"] ?? "";
  control.dataset["value"] = value;

  const current = control.querySelector<HTMLElement>(
    "[data-field='menu-current']",
  );
  if (current !== null) current.textContent = item.textContent?.trim() ?? "";

  for (const option of control.querySelectorAll<HTMLElement>(
    "[data-menu-item]",
  )) {
    const isChosen = option === item;
    option.setAttribute("aria-checked", String(isChosen));
    const tick = option.querySelector<HTMLElement>("[data-option-tick]");
    if (tick !== null) tick.hidden = !isChosen;
  }
}

/**
 * Selects the option holding `value`.
 *
 * @returns Whether the control offers it. `false` means the caller asked for a
 *   value this control cannot represent — a role retired since the page was
 *   rendered, say — and the control is left alone rather than shown holding
 *   something no option ticks. Callers that need a fallback pass `""`.
 */
export function setMenuValue(control: HTMLElement, value: string): boolean {
  const item = control.querySelector<HTMLElement>(
    `[data-menu-item][data-value="${CSS.escape(value)}"]`,
  );
  if (item === null) return false;
  applyMenuChoice(control, item);
  return true;
}

/** The options a control currently offers, in the order it offers them. */
export function menuOptions(control: HTMLElement): readonly MenuOption[] {
  return [...control.querySelectorAll<HTMLElement>("[data-menu-item]")].map(
    (item) => ({
      value: item.dataset["value"] ?? "",
      label: item.textContent?.trim() ?? "",
    }),
  );
}

/**
 * Replaces the options a control offers, keeping its value when it survives.
 *
 * Each row is cloned from one the server rendered rather than built here: the
 * tick is an inlined icon, and a second copy of that markup in TypeScript is a
 * copy that drifts the day the icon changes. A control the server rendered
 * empty therefore cannot be filled, which is why every `MenuSelect` ships with
 * at least its "any" entry.
 *
 * @param control - The `[data-menu-select]` wrapper.
 * @param options - The full list, including the leading "any"/"none" entry.
 * @param fallback - The value to select when the current one is gone; the first
 *   option when that one is gone too, because a control holding a value none of
 *   its rows tick is the one state this must never leave behind.
 */
export function setMenuOptions(
  control: HTMLElement,
  options: readonly MenuOption[],
  fallback = "",
): void {
  const panel = control.querySelector<HTMLElement>("[data-menu-panel]");
  const prototype = control.querySelector<HTMLElement>("[data-menu-item]");
  if (panel === null || prototype === null || options.length === 0) return;

  const chosen = control.dataset["value"] ?? "";
  const rows = options.map((option) => {
    const row = prototype.cloneNode(true);
    if (!(row instanceof HTMLElement)) return null;
    row.dataset["value"] = option.value;
    const label = row.querySelector<HTMLElement>(
      "span:not([data-option-tick])",
    );
    if (label !== null) label.textContent = option.label;
    return row;
  });
  const rendered = rows.filter((row) => row !== null);
  panel.replaceChildren(...rendered);

  if (setMenuValue(control, chosen)) return;
  if (setMenuValue(control, fallback)) return;
  const first = rendered[0];
  if (first !== undefined) applyMenuChoice(control, first);
}
