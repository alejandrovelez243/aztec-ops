/**
 * The two hands every island patches with: clone the markup the server already
 * rendered, and fill it by name.
 *
 * Rows that appear after the first paint — a note that arrived over the stream,
 * a page of the feed loaded live — are cloned from a `<template>` emitted by the
 * same Astro component that renders the server-side rows, and filled through
 * `data-field` hooks. The markup therefore has exactly one home: a second copy
 * written as a template string in TypeScript is how a live row starts drifting
 * from its server-rendered siblings, one class at a time.
 *
 * Framework-free (no Astro import) and shared by every island, which is why it
 * lives in `lib/` rather than under one view's directory.
 */

/**
 * Clones the markup of `<template data-template="name">` inside `root`.
 *
 * @returns The first element of the clone, or `null` when the template is
 *   absent — an island script runs on every navigation, so a page that does not
 *   host the template must produce a no-op rather than an exception.
 */
export function cloneTemplate(
  root: ParentNode,
  name: string,
): HTMLElement | null {
  const template = root.querySelector(`template[data-template="${name}"]`);
  if (!(template instanceof HTMLTemplateElement)) return null;
  const fragment = template.content.cloneNode(true);
  if (!(fragment instanceof DocumentFragment)) return null;
  const first = fragment.firstElementChild;
  return first instanceof HTMLElement ? first : null;
}

/**
 * Writes text into every `[data-field="name"]` inside `scope`.
 *
 * Text, never HTML: the values are operator prose — a blocker description, an
 * override's reason — and `innerHTML` would make every one of them a script tag.
 */
export function setField(scope: ParentNode, name: string, value: string): void {
  for (const node of scope.querySelectorAll(`[data-field="${name}"]`)) {
    node.textContent = value;
  }
}
