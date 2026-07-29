/**
 * Pure formatters for the priorities surface — no DOM, no Astro, no network.
 *
 * Every string a person reads here is Spanish (CLAUDE.md §Language); every number
 * that lands in a repeated position is rendered so `tabular-nums` can align it.
 * Prose that arrives from the API (a risk `reason`, a signal `reason`) is *not*
 * translated here: it is data, and inventing a Spanish sentence for it would put
 * words in the engine's mouth.
 */
import type { ToneClass } from "./tone";

/** Ranks below this render with a leading zero, so the plate is always two glyphs. */
const RANK_PAD_BELOW = 10;

/** Days from today inside which a deadline stops being routine and starts pressing. */
const FINAL_WEEK_DAYS = 7;

const DAY_MS = 86_400_000;

/**
 * Spanish, UTC: the API sends `date` fields as `YYYY-MM-DD`, which parse as UTC
 * midnight. Formatting them in the reader's zone would move a deadline by a day for
 * anyone west of Greenwich.
 */
const DATE_FORMAT = new Intl.DateTimeFormat("es", {
  day: "numeric",
  month: "short",
  year: "numeric",
  timeZone: "UTC",
});

/**
 * Two-digit rank plate text.
 *
 * Ranks of 100 and above render unpadded rather than truncated: a queue that outgrew
 * two digits is a real state, and dropping the hundreds digit would silently reorder
 * the plate against the list it labels.
 */
export function formatRank(rank: number): string {
  const whole = Math.max(1, Math.round(rank));
  return whole < RANK_PAD_BELOW ? `0${whole}` : String(whole);
}

/**
 * One signal's contribution, at most one decimal.
 *
 * Same rounding as the score it sums into, so a breakdown never appears to
 * disagree with the figure above it by a rounding artefact. Never recomputed from
 * `raw * weight`: the server computed it with `Decimal` and this is a readout.
 */
export function formatPoints(contribution: number): string {
  const rounded = Math.round(contribution * 10) / 10;
  return (Object.is(rounded, -0) ? 0 : rounded).toString();
}

/**
 * How the queue's due column reads for one project.
 *
 * Invariant: `figure` is `null` exactly when `kind` is `"absent"` — the absence has
 * no number to align, and the chip renders the named condition instead of an empty
 * cell (DESIGN.md, The Present-Absence Rule).
 */
export interface DueChip {
  readonly kind: DueKind;
  /** Spanish lead-in, e.g. `vence en`. */
  readonly lead: string;
  /** Tabular part, e.g. `3 d` or `hoy`; `null` only for `"absent"`. */
  readonly figure: string | null;
  /** Full sentence for `title` and the accessible name. */
  readonly detail: string;
}

/** The five ways a target date can read. */
export type DueKind = "absent" | "overdue" | "today" | "soon" | "later";

/** Tone per due kind: the deadline's own urgency, never the brand voice. */
const DUE_TONE: Readonly<Record<DueKind, ToneClass>> = {
  absent: "tone-ambar",
  overdue: "tone-rojo",
  today: "tone-ambar",
  soon: "tone-ambar",
  later: "tone-piedra",
};

/** The tone class one due kind renders with. */
export function toneForDue(kind: DueKind): ToneClass {
  return DUE_TONE[kind];
}

/**
 * Describes a project's target date as the chip the queue renders.
 *
 * A `null` date is the `NO_TARGET_DATE` signal itself, so it becomes the named
 * absence "sin fecha" and never today's date — substituting one would erase a real
 * risk and quietly change `deadline_pressure` from 0.5 to something invented.
 *
 * Distance is whole calendar days on the UTC clock, so a DST change or the reader's
 * local time of day can never shift the readout by one.
 */
export function describeDue(targetDate: string | null, now?: Date): DueChip {
  if (targetDate === null) {
    return {
      kind: "absent",
      lead: "sin fecha",
      figure: null,
      detail: "Sin fecha objetivo comprometida.",
    };
  }
  const target = new Date(targetDate);
  const days = calendarDayDiff(target, now ?? new Date());
  const when = DATE_FORMAT.format(target);
  if (days < 0) {
    const late = Math.abs(days);
    return {
      kind: "overdue",
      lead: "vencido",
      figure: `${late} d`,
      detail: `Venció el ${when}, hace ${plural(late, "día", "días")}.`,
    };
  }
  if (days === 0) {
    return {
      kind: "today",
      lead: "vence",
      figure: "hoy",
      detail: `La fecha objetivo es hoy, ${when}.`,
    };
  }
  return {
    kind: days <= FINAL_WEEK_DAYS ? "soon" : "later",
    lead: "vence en",
    figure: `${days} d`,
    detail: `Fecha objetivo ${when}, dentro de ${plural(days, "día", "días")}.`,
  };
}

/**
 * How wide one signal's bar is drawn, as a 0–1 ratio of the widest bar beside it.
 *
 * Relative and not absolute, because a breakdown's job is to show which signal
 * carried the decision; an absolute 0–100 scale would render every bar as a sliver
 * on a low-scoring project and hide exactly that. Magnitude is used, so a negative
 * contribution draws as far as a positive one of the same size.
 */
export function barRatio(contribution: number, widest: number): number {
  if (widest <= 0) return 0;
  return Math.min(1, Math.abs(contribution) / widest);
}

/** The widest magnitude in a breakdown; `0` when the breakdown is empty. */
export function widestContribution(contributions: readonly number[]): number {
  let widest = 0;
  for (const value of contributions) {
    const magnitude = Math.abs(value);
    if (magnitude > widest) widest = magnitude;
  }
  return widest;
}

/**
 * A local wall-clock time for the staleness marker, e.g. `14:05`.
 *
 * Deliberately the reader's own zone and not UTC: this answers "how old is what I am
 * looking at", which is a question about the reader's clock. Returns `null` for a
 * value that is not a parseable instant, so the caller renders the marker without a
 * time rather than the string `Invalid Date`.
 */
export function formatClock(instant: string): string | null {
  const moment = new Date(instant);
  if (Number.isNaN(moment.getTime())) return null;
  return new Intl.DateTimeFormat("es", {
    hour: "2-digit",
    minute: "2-digit",
  }).format(moment);
}

/**
 * A date and wall-clock time for "calculado el …", e.g. `28 jul, 14:05`.
 *
 * Rendered in the reader's own zone, unlike {@link describeDue}: this is a moment in
 * time the operator compares against their own clock, not a calendar commitment.
 * A value that is not a parseable instant returns `null`, so the caller can render
 * the field as unknown rather than the string `Invalid Date`.
 */
export function formatInstant(instant: string): string | null {
  const moment = new Date(instant);
  if (Number.isNaN(moment.getTime())) return null;
  return new Intl.DateTimeFormat("es", {
    day: "numeric",
    month: "short",
    hour: "2-digit",
    minute: "2-digit",
  }).format(moment);
}

/**
 * The DOM id of one queue row's button.
 *
 * Derived from the project code rather than from the row's index, because the island
 * reorders rows on every recalculation and an index-derived id would point at a
 * different project after the first FLIP — silently breaking `aria-controls`.
 */
export function rowDomId(code: string): string {
  return `queue-row-${code}`;
}

/** The DOM id of the context panel that row controls. Same stability argument. */
export function detailDomId(code: string): string {
  return `queue-detail-${code}`;
}

/** `1 día` / `3 días`: Spanish plural without pulling in an i18n runtime. */
function plural(count: number, one: string, many: string): string {
  return `${count} ${count === 1 ? one : many}`;
}

/**
 * Whole calendar days from `from` to `to`, both normalised to UTC midnight, so
 * `23:59` against `00:01` the next day is one day and a same-day pair is zero.
 */
function calendarDayDiff(to: Date, from: Date): number {
  const toMidnight = Date.UTC(
    to.getUTCFullYear(),
    to.getUTCMonth(),
    to.getUTCDate(),
  );
  const fromMidnight = Date.UTC(
    from.getUTCFullYear(),
    from.getUTCMonth(),
    from.getUTCDate(),
  );
  return Math.round((toMidnight - fromMidnight) / DAY_MS);
}
