/**
 * The islands' hands: template cloning, field filling and envelope reading.
 *
 * Rows that appear after the first paint — a blocker somebody raised, a note
 * that arrived over the stream — are cloned from a `<template>` rendered by the
 * same Astro component that renders the server-side rows, and filled through
 * `data-field` hooks. The markup therefore has exactly one home: a second copy
 * written as a template string in TypeScript is how a live row starts drifting
 * from its server-rendered siblings, one class at a time.
 *
 * Envelope payloads arrive as `Record<string, unknown>` because the stream store
 * does not interpret them (`lib/stream/store.ts`); the readers below narrow one
 * field at a time and return `null` rather than throwing, so a payload that
 * grew a field cannot take an island down.
 */

import type { ToneSpec } from "./tone";

/** Every tone class a patched element might currently be wearing. */
const TONE_CLASSES = [
  "tone-rojo",
  "tone-ambar",
  "tone-verde",
  "tone-cielo",
  "tone-piedra",
  "tone-data",
];

/**
 * Clones the markup of `<template data-template="name">` inside `root`.
 *
 * @returns The first element of the clone, or `null` when the template is
 *   absent — an island rendered on a page that does not host it must no-op, not
 *   throw, because every island script runs on every navigation.
 */
export function cloneTemplate(
  root: ParentNode,
  name: string,
): HTMLElement | null {
  const template = root.querySelector(`template[data-template="${name}"]`);
  if (!(template instanceof HTMLTemplateElement)) return null;
  const fragment = template.content.cloneNode(true);
  const first = fragment.firstElementChild;
  return first instanceof HTMLElement ? first : null;
}

/**
 * Writes text into every `[data-field="name"]` inside `scope`.
 *
 * Text, never HTML: values here are operator prose (a blocker description, a
 * note body) and `innerHTML` would make every one of them a script tag.
 */
export function setField(scope: ParentNode, name: string, value: string): void {
  for (const node of scope.querySelectorAll(`[data-field="${name}"]`)) {
    node.textContent = value;
  }
}

/** Sets an attribute on every `[data-field="name"]` inside `scope`. */
export function setFieldAttribute(
  scope: ParentNode,
  name: string,
  attribute: string,
  value: string,
): void {
  for (const node of scope.querySelectorAll(`[data-field="${name}"]`)) {
    node.setAttribute(attribute, value);
  }
}

/**
 * Repaints one element with a tone, dropping whichever tone it wore before.
 *
 * The inline `--tone-solid` is removed when the new tone is semantic; leaving a
 * stale one behind would make `.tone-data` derive its ink from the previous
 * state's colour.
 */
export function applyTone(element: HTMLElement, tone: ToneSpec): void {
  element.classList.remove(...TONE_CLASSES);
  element.classList.add(tone.className);
  if (tone.solid === null) {
    element.style.removeProperty("--tone-solid");
    return;
  }
  element.style.setProperty("--tone-solid", tone.solid);
}

/**
 * Whether an envelope is newer than what the DOM currently shows.
 *
 * Both values are ISO-8601 UTC, so lexicographic comparison is chronological.
 * A missing rendered stamp counts as older: a region that never recorded when
 * it was built cannot claim to be ahead of the bus.
 *
 * Skipping this check is what makes a slow duplicate delivery overwrite a newer
 * state with an older one — at-least-once delivery guarantees it will happen.
 */
export function isFresher(
  occurredAt: string,
  renderedAt: string | null | undefined,
): boolean {
  if (renderedAt === null || renderedAt === undefined || renderedAt === "") {
    return true;
  }
  return occurredAt > renderedAt;
}

/** Reads a string field out of an envelope payload; `null` when absent. */
export function readString(
  payload: Record<string, unknown>,
  key: string,
): string | null {
  const value = payload[key];
  return typeof value === "string" ? value : null;
}

/** Reads a numeric field out of an envelope payload; `null` when absent. */
export function readNumber(
  payload: Record<string, unknown>,
  key: string,
): number | null {
  const value = payload[key];
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

/**
 * The one authored acknowledgement of a remote change: the touched surface
 * pulses a Cobalto ring (DESIGN.md §Motion, "the remote move").
 *
 * A no-op under reduced motion — the colour and the patched value already
 * carry the information; the ring only says "this moved while you watched".
 */
export function pulse(element: HTMLElement): void {
  if (
    typeof window === "undefined" ||
    window.matchMedia("(prefers-reduced-motion: reduce)").matches
  ) {
    return;
  }
  element.animate(
    [
      { boxShadow: "0 0 0 0 var(--cobalto-papel)" },
      { boxShadow: "0 0 0 6px var(--cobalto-papel)", offset: 0.35 },
      { boxShadow: "0 0 0 0 var(--cobalto-papel)" },
    ],
    { duration: 900, easing: "ease-out" },
  );
}

/**
 * Marks a control as in-flight: the pressed button hosts the spinner and stops
 * accepting input, so a surface never freezes silently (DESIGN.md §Motion).
 *
 * `disabled` is set beside the class because `.is-loading` only removes pointer
 * events — a keyboard `Enter` would otherwise fire the action twice.
 */
export function setPending(button: HTMLButtonElement, pending: boolean): void {
  button.classList.toggle("is-loading", pending);
  button.disabled = pending;
}
