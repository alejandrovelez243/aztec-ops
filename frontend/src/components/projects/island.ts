/**
 * Island lifecycle under the view-transition router.
 *
 * A component's `<script>` module is evaluated **once per app**, not once per
 * navigation: after the first swap the module is already loaded and its
 * top-level code never runs again. Every island therefore does its DOM work
 * inside `astro:page-load` (which fires on the first load *and* after every
 * swap) and releases it on `astro:before-swap`.
 *
 * What this prevents: subscriptions that outlive the DOM they patch. A handler
 * kept across a swap keeps writing into detached nodes, holds the shared SSE
 * connection open for a page that shows none of it, and — because the mount
 * runs again on the way back — stacks a second copy of itself every time the
 * operator navigates.
 */

/**
 * Mounts `mount(root)` on every page that contains `selector`, and calls the
 * teardown it returns before the next swap.
 *
 * @param selector - Root of the island; absent on pages that do not host it,
 *   in which case nothing is mounted.
 * @param mount - Wires the island and returns its teardown.
 */
export function onPage(
  selector: string,
  mount: (root: HTMLElement) => () => void,
): void {
  let teardown: (() => void) | null = null;

  const release = (): void => {
    teardown?.();
    teardown = null;
  };

  document.addEventListener("astro:before-swap", release);
  document.addEventListener("astro:page-load", () => {
    // Defensive: a mount without a preceding swap (a re-entered page) must
    // replace its predecessor rather than stack a second set of handlers.
    release();
    const root = document.querySelector<HTMLElement>(selector);
    if (root === null) return;
    teardown = mount(root);
  });
}
