/**
 * Tab switching for the project detail.
 *
 * Local state with no server truth behind it, so it is allowed to lead
 * (`docs/standards/PATTERNS_FRONTEND.md` §8): nothing here can be refused.
 *
 * Keyboard behaviour follows the ARIA tabs pattern — arrows move and select,
 * Home/End jump to the ends, and exactly one tab is in the tab order at a time
 * (roving `tabindex`), so Tab moves *past* the tablist into the panel rather
 * than walking through four buttons first.
 */

/**
 * Mounts one tablist.
 *
 * @param root - The element carrying `data-tabs`.
 * @returns The teardown; removes the delegated listeners.
 */
export function mountTabs(root: HTMLElement): () => void {
  const controller = new AbortController();
  const { signal } = controller;
  const tabs = [...root.querySelectorAll<HTMLElement>("[data-tab]")];

  const select = (key: string, focus: boolean): void => {
    for (const tab of tabs) {
      const isSelected = tab.dataset.tab === key;
      tab.setAttribute("aria-selected", String(isSelected));
      tab.tabIndex = isSelected ? 0 : -1;
      if (isSelected && focus) tab.focus();
    }
    for (const panel of root.querySelectorAll<HTMLElement>("[data-panel]")) {
      panel.hidden = panel.dataset.panel !== key;
    }
  };

  root.addEventListener(
    "click",
    (event) => {
      const target = event.target;
      if (!(target instanceof Element)) return;
      const tab = target.closest<HTMLElement>("[data-tab]");
      const key = tab?.dataset.tab;
      if (key !== undefined) select(key, false);
    },
    { signal },
  );

  root.addEventListener(
    "keydown",
    (event) => {
      const target = event.target;
      if (!(target instanceof Element)) return;
      const current = target.closest<HTMLElement>("[data-tab]");
      if (current === null) return;

      const index = tabs.indexOf(current);
      const next = nextIndex(event.key, index, tabs.length);
      if (next === null) return;
      event.preventDefault();
      const key = tabs[next]?.dataset.tab;
      if (key !== undefined) select(key, true);
    },
    { signal },
  );

  return () => {
    controller.abort();
  };
}

/** Where a key moves the selection, or `null` when the key is not ours. */
function nextIndex(key: string, index: number, length: number): number | null {
  if (length === 0) return null;
  switch (key) {
    case "ArrowRight":
      return (index + 1) % length;
    case "ArrowLeft":
      return (index - 1 + length) % length;
    case "Home":
      return 0;
    case "End":
      return length - 1;
    default:
      return null;
  }
}
