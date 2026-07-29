/**
 * The popup grammar the row controls share: one trigger, one panel, one choice.
 *
 * Written once because the owner picker and the task's move menu differ only in
 * what they put inside the panel — copying the open/close/keyboard code into
 * both is how one of them silently stops answering `Escape`.
 *
 * Three properties this owes the rest of the surface:
 *
 * - **Delegated, never per row.** One listener per container, so a task row that
 *   arrives over the stream after the first paint is operable the moment it is
 *   appended; nothing has to remember to mount it.
 * - **Keyboard first.** The trigger is a real `<button>`, the items are real
 *   `<button>`s inside `role="menu"`, `ArrowUp`/`ArrowDown`/`Home`/`End` walk
 *   them, `Escape` closes and hands focus back. A control that only answers a
 *   pointer is not an alternative to the drag it replaces.
 * - **One menu at a time**, tracked module-wide: two open panels on one screen
 *   is two answers to the same question.
 *
 * The panel's *content* is the caller's business ({@link MenuHooks.fill}), and
 * so is what a choice does ({@link MenuHooks.choose}). This module never
 * fetches, never writes and never paints a result.
 */

/** What a mounted menu does when it opens and when an item is picked. */
export interface MenuHooks {
  /**
   * Fills `panel` for the control that is opening; awaited before focus moves.
   *
   * Called on **every** open, not once: the roster can have changed and a row's
   * legal moves certainly have. An implementation that renders its options
   * server-side may simply return.
   */
  readonly fill: (control: HTMLElement, panel: HTMLElement) => Promise<void>;
  /**
   * Handles the chosen `[data-menu-item]`. The menu is already closed; the hook
   * owns the pending state, the request and the paint.
   */
  readonly choose: (control: HTMLElement, item: HTMLElement) => Promise<void>;
}

/** The panel currently on screen, or `null` when every menu is closed. */
interface OpenMenu {
  readonly control: HTMLElement;
  readonly panel: HTMLElement;
  readonly trigger: HTMLButtonElement;
}

let open: OpenMenu | null = null;

/** Gap between the trigger and its panel, in px; matches `--space-1`. */
const PANEL_OFFSET = 4;

/**
 * Anchors the panel to its trigger in viewport coordinates.
 *
 * `position: fixed` rather than `absolute` because the task table is a
 * horizontal scroller: a panel positioned inside it is clipped by it, and the
 * last row's menu would open into a cut-off strip. Fixed escapes the container;
 * the cost is that the panel must be closed when the page scrolls, which
 * {@link mountMenus} does.
 *
 * The panel flips above the trigger when the space below cannot hold it, and is
 * pulled back inside the viewport horizontally, so a control at the right edge
 * of a wide table still opens something readable.
 */
function placePanel(trigger: HTMLElement, panel: HTMLElement): void {
  const anchor = trigger.getBoundingClientRect();
  const { width, height } = panel.getBoundingClientRect();
  const below = window.innerHeight - anchor.bottom - PANEL_OFFSET;

  const top =
    below >= height || anchor.top < height
      ? anchor.bottom + PANEL_OFFSET
      : anchor.top - height - PANEL_OFFSET;
  const left = Math.max(
    PANEL_OFFSET,
    Math.min(anchor.left, window.innerWidth - width - PANEL_OFFSET),
  );

  panel.style.top = `${top}px`;
  panel.style.left = `${left}px`;
}

/** The focusable items of a panel, in DOM order. */
function itemsOf(panel: HTMLElement): readonly HTMLButtonElement[] {
  return [
    ...panel.querySelectorAll<HTMLButtonElement>(
      "[data-menu-item]:not(:disabled)",
    ),
  ];
}

/**
 * Closes whatever is open.
 *
 * @param focusTrigger - Whether focus returns to the trigger. True for
 *   `Escape` and for a completed choice — the operator's place on the row is
 *   preserved; false for a click elsewhere, which has already moved focus.
 */
export function closeMenu(focusTrigger: boolean): void {
  if (open === null) return;
  const { panel, trigger } = open;
  open = null;
  panel.hidden = true;
  trigger.setAttribute("aria-expanded", "false");
  if (focusTrigger) trigger.focus();
}

/** Whether `node` lives inside the control that currently owns the open panel. */
function isInsideOpen(node: Node): boolean {
  return open !== null && open.control.contains(node);
}

async function openMenu(
  control: HTMLElement,
  trigger: HTMLButtonElement,
  hooks: MenuHooks,
): Promise<void> {
  const panel = control.querySelector<HTMLElement>("[data-menu-panel]");
  if (panel === null) return;

  closeMenu(false);
  open = { control, panel, trigger };
  panel.hidden = false;
  trigger.setAttribute("aria-expanded", "true");
  placePanel(trigger, panel);

  await hooks.fill(control, panel);

  // The operator can have closed it while `fill` was awaiting a response.
  if (open?.panel !== panel) return;
  // Measured again: the panel just changed height, from one loading line to a
  // roster, and an anchor computed against the old box would sit wrong.
  placePanel(trigger, panel);
  itemsOf(panel)[0]?.focus();
}

/** Moves focus within the open panel; `step` of +1 is "next", -1 is "previous". */
function moveFocus(step: number): void {
  if (open === null) return;
  const items = itemsOf(open.panel);
  if (items.length === 0) return;
  const active = document.activeElement;
  const index = items.findIndex((item) => item === active);
  const next = index === -1 ? 0 : (index + step + items.length) % items.length;
  items[next]?.focus();
}

/**
 * Wires every `controlSelector` inside `container`.
 *
 * @param container - The delegation root; must outlive the controls inside it.
 * @param controlSelector - Selects one control, e.g. `[data-owner-control]`.
 * @param hooks - What to render and what a choice means.
 * @returns The teardown; drops the listeners and closes any open panel.
 */
export function mountMenus(
  container: HTMLElement,
  controlSelector: string,
  hooks: MenuHooks,
): () => void {
  const controller = new AbortController();
  const { signal } = controller;

  container.addEventListener(
    "click",
    (event) => {
      const target = event.target;
      if (!(target instanceof Element)) return;

      const item = target.closest<HTMLButtonElement>("[data-menu-item]");
      if (item !== null && !item.disabled) {
        const control = item.closest<HTMLElement>(controlSelector);
        if (control === null) return;
        closeMenu(true);
        void hooks.choose(control, item);
        return;
      }

      const trigger = target.closest<HTMLButtonElement>("[data-menu-trigger]");
      if (trigger === null || trigger.disabled) return;
      const control = trigger.closest<HTMLElement>(controlSelector);
      if (control === null) return;
      if (open?.trigger === trigger) {
        closeMenu(true);
        return;
      }
      void openMenu(control, trigger, hooks);
    },
    { signal },
  );

  container.addEventListener(
    "keydown",
    (event) => {
      const target = event.target;
      if (!(target instanceof Element)) return;

      if (event.key === "Escape" && open !== null && isInsideOpen(target)) {
        event.preventDefault();
        closeMenu(true);
        return;
      }

      const trigger = target.closest<HTMLButtonElement>("[data-menu-trigger]");
      if (trigger !== null && !trigger.disabled && event.key === "ArrowDown") {
        const control = trigger.closest<HTMLElement>(controlSelector);
        if (control === null) return;
        event.preventDefault();
        void openMenu(control, trigger, hooks);
        return;
      }

      if (open === null || !isInsideOpen(target)) return;
      if (event.key === "ArrowDown") {
        event.preventDefault();
        moveFocus(1);
        return;
      }
      if (event.key === "ArrowUp") {
        event.preventDefault();
        moveFocus(-1);
      }
    },
    { signal },
  );

  // A click anywhere else dismisses. Bubbling rather than capturing, so the
  // container's own handler has already opened the panel by the time this runs
  // and the containment check below can recognise it.
  document.addEventListener(
    "click",
    (event) => {
      const target = event.target;
      if (target instanceof Node && isInsideOpen(target)) return;
      closeMenu(false);
    },
    { signal },
  );

  // A fixed panel does not travel with its trigger, so scrolling or resizing
  // dismisses instead of leaving a menu floating beside nothing. Capture, so a
  // scroll inside the task table counts as much as one on the page.
  const dismiss = (): void => {
    closeMenu(false);
  };
  document.addEventListener("scroll", dismiss, {
    signal,
    capture: true,
    passive: true,
  });
  window.addEventListener("resize", dismiss, { signal, passive: true });

  return () => {
    controller.abort();
    closeMenu(false);
  };
}
