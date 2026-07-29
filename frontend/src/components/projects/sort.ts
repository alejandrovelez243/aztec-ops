/**
 * The orders `/projects` can be read in, and how each one compares two rows.
 *
 * One module rather than a list in the toolbar and a `switch` in the island:
 * the menu's labels and the comparator that answers them would otherwise be
 * free to disagree, and a fourth order would mean editing two files.
 *
 * The comparators read the `data-sort-*` attributes the card and the row both
 * carry, so the sort works over the DOM it was handed instead of a second copy
 * of the list it would have to keep in step.
 */

/** The orders the toolbar offers. `name` is what the server rendered. */
export type SortMode = "name" | "score" | "due";

/** One entry of the sort menu, in the order it is offered. */
export interface SortOption {
  readonly value: SortMode;
  readonly label: string;
}

/** The menu, first entry first — and the default. */
export const SORT_OPTIONS: readonly SortOption[] = [
  { value: "name", label: "Nombre (A-Z)" },
  { value: "score", label: "Prioridad (mayor primero)" },
  { value: "due", label: "Fecha objetivo (más urgente)" },
];

/** A project with no target date sorts after every dated one, never before. */
const NO_DATE = Number.POSITIVE_INFINITY;

/** The name a row sorts under, pre-folded by the server render. */
function sortName(element: HTMLElement): string {
  return element.dataset.sortName ?? "";
}

/** The score a row sorts under; an unparseable one sinks to the bottom. */
function sortScore(element: HTMLElement): number {
  const value = Number.parseFloat(element.dataset.sortScore ?? "");
  return Number.isFinite(value) ? value : Number.NEGATIVE_INFINITY;
}

/** The deadline as a sortable number; "no date" is an absence, not a zero. */
function sortDue(element: HTMLElement): number {
  const raw = element.dataset.sortDue ?? "";
  if (raw === "") return NO_DATE;
  const at = Date.parse(raw);
  return Number.isNaN(at) ? NO_DATE : at;
}

/** Alphabetical in Spanish, which is also the tiebreak of every other order. */
function byName(a: HTMLElement, b: HTMLElement): number {
  return sortName(a).localeCompare(sortName(b), "es");
}

/** One comparator per order. */
export const COMPARATORS: Readonly<
  Record<SortMode, (a: HTMLElement, b: HTMLElement) => number>
> = {
  name: byName,
  score: (a, b) => sortScore(b) - sortScore(a) || byName(a, b),
  due: (a, b) => sortDue(a) - sortDue(b) || byName(a, b),
};

/**
 * Narrows an arbitrary string to an order.
 *
 * Anything unknown — a stale value in storage, a hand-edited attribute — falls
 * back to the order the server rendered, so the surface never ends up sorted by
 * a comparator that does not exist.
 */
export function toSortMode(value: string): SortMode {
  return value === "score" || value === "due" ? value : "name";
}

/** The label the trigger shows for one order. */
export function sortLabel(mode: SortMode): string {
  return SORT_OPTIONS.find((option) => option.value === mode)?.label ?? "";
}
