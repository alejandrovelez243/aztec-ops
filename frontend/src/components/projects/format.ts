/**
 * Pure formatters for the projects surfaces: dates, deadlines, relative time
 * and scores. Framework-free and side-effect-free, so the same function formats
 * a cell during the server render and after an SSE patch — two formatters would
 * eventually disagree on the same value, which is how a row starts contradicting
 * itself.
 *
 * All output is Spanish (CLAUDE.md §Language); every identifier is English.
 *
 * Date handling invariant: the API sends calendar dates as `YYYY-MM-DD`
 * (`docs/API.md` §1.6). `new Date("2026-08-12")` parses that as UTC midnight,
 * which renders as the 11th anywhere west of Greenwich — a deadline off by one
 * day. Calendar dates are therefore parsed field by field into local midnight,
 * and only instants (`...Z`) go through the native parser.
 */

import type { ToneClass } from "./tone";

const DAY_MS = 86_400_000;

/** Deadlines closer than this render as a warning rather than a plain date. */
const SOON_DAYS = 7;

const dayFormat = new Intl.DateTimeFormat("es", {
  day: "numeric",
  month: "short",
  year: "numeric",
});

const instantFormat = new Intl.DateTimeFormat("es", {
  day: "numeric",
  month: "short",
  year: "numeric",
  hour: "2-digit",
  minute: "2-digit",
});

const relativeFormat = new Intl.RelativeTimeFormat("es", { numeric: "auto" });

/**
 * Parses a calendar date (or the date part of an instant) into local midnight.
 *
 * @returns `null` for absent or unparseable input — absence is a signal the
 *   caller must render, never a silent "today".
 */
export function parseDay(value: string | null | undefined): Date | null {
  if (value === null || value === undefined || value === "") return null;
  const match = /^(\d{4})-(\d{2})-(\d{2})/.exec(value);
  if (match === null) return null;
  const [, year, month, day] = match;
  if (year === undefined || month === undefined || day === undefined) {
    return null;
  }
  return new Date(Number(year), Number(month) - 1, Number(day));
}

/** Parses an ISO instant; `null` when absent or unparseable. */
export function parseInstant(value: string | null | undefined): Date | null {
  if (value === null || value === undefined || value === "") return null;
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? null : parsed;
}

/** A calendar date as "12 ago 2026", or `null` when there is no date. */
export function formatDay(value: string | null | undefined): string | null {
  const parsed = parseDay(value);
  return parsed === null ? null : dayFormat.format(parsed);
}

/** An instant as "28 jul 2026, 09:05" — the `title` behind every relative time. */
export function formatInstant(value: string | null | undefined): string | null {
  const parsed = parseInstant(value);
  return parsed === null ? null : instantFormat.format(parsed);
}

/**
 * Whole days between two instants, counted on local calendar midnights so
 * "vence mañana" does not become "vence en 0 días" at 23:00.
 */
export function daysBetween(from: Date, to: Date): number {
  const a = new Date(from.getFullYear(), from.getMonth(), from.getDate());
  const b = new Date(to.getFullYear(), to.getMonth(), to.getDate());
  return Math.round((b.getTime() - a.getTime()) / DAY_MS);
}

/**
 * A past instant as "hace 5 minutos" / "hace 3 días".
 *
 * Future instants read "en 2 días" — the same formatter, because an activity
 * record with a clock skew must not silently render as the past.
 */
export function relativeTime(
  value: string | null | undefined,
  now: Date = new Date(),
): string | null {
  const parsed = parseInstant(value);
  if (parsed === null) return null;
  const seconds = (parsed.getTime() - now.getTime()) / 1000;
  const magnitude = Math.abs(seconds);
  if (magnitude < 60) return relativeFormat.format(Math.round(seconds), "second");
  if (magnitude < 3600) {
    return relativeFormat.format(Math.round(seconds / 60), "minute");
  }
  if (magnitude < 86_400) {
    return relativeFormat.format(Math.round(seconds / 3600), "hour");
  }
  if (magnitude < 2_592_000) {
    return relativeFormat.format(Math.round(seconds / 86_400), "day");
  }
  if (magnitude < 31_536_000) {
    return relativeFormat.format(Math.round(seconds / 2_592_000), "month");
  }
  return relativeFormat.format(Math.round(seconds / 31_536_000), "year");
}

/**
 * How a target date reads on a chip.
 *
 * One discriminated union instead of a date plus an `isOverdue` boolean plus a
 * `hasDate` boolean: "overdue with no date" and "absent but due in 3 days" are
 * states this type cannot represent (CLAUDE.md rule 13). `absent` is a
 * first-class arm because a missing deadline *is* the `NO_TARGET_DATE` signal
 * (Present-Absence Rule), not a blank cell.
 */
export type DueState =
  | { readonly kind: "absent"; readonly label: string; readonly tone: ToneClass }
  | {
      readonly kind: "overdue";
      readonly label: string;
      readonly tone: ToneClass;
      readonly title: string;
      readonly days: number;
    }
  | {
      readonly kind: "today";
      readonly label: string;
      readonly tone: ToneClass;
      readonly title: string;
    }
  | {
      readonly kind: "soon";
      readonly label: string;
      readonly tone: ToneClass;
      readonly title: string;
      readonly days: number;
    }
  | {
      readonly kind: "scheduled";
      readonly label: string;
      readonly tone: ToneClass;
      readonly title: string;
    };

/**
 * Reads a target date against today's calendar.
 *
 * The absent arm is ámbar and says so in words ("sin fecha"), because the
 * project with no deadline is the one nobody notices slipping.
 */
export function dueState(
  target: string | null | undefined,
  now: Date = new Date(),
): DueState {
  const day = parseDay(target);
  if (day === null) {
    return { kind: "absent", label: "Sin fecha", tone: "tone-ambar" };
  }
  const title = dayFormat.format(day);
  const days = daysBetween(now, day);
  if (days < 0) {
    const overdue = Math.abs(days);
    return {
      kind: "overdue",
      label: `Venció hace ${overdue} d`,
      tone: "tone-rojo",
      title,
      days: overdue,
    };
  }
  if (days === 0) {
    return { kind: "today", label: "Vence hoy", tone: "tone-rojo", title };
  }
  if (days <= SOON_DAYS) {
    return {
      kind: "soon",
      label: `Vence en ${days} d`,
      tone: "tone-ambar",
      title,
      days,
    };
  }
  return { kind: "scheduled", label: title, tone: "tone-piedra", title };
}

/**
 * A 0–100 score with one decimal, the precision the engine persists.
 *
 * Rounding to an integer here would make two projects that the ranking
 * separates look tied, which is the one thing the queue exists to answer.
 */
export function formatScore(value: number): string {
  return value.toFixed(1);
}

/** A signal's contribution, signed so a penalty reads as one. */
export function formatContribution(value: number): string {
  const rendered = Math.abs(value).toFixed(1);
  return value < 0 ? `−${rendered}` : `+${rendered}`;
}

/**
 * A signal's share of the total, as a 0–100 bar width.
 *
 * Clamped: the policy may weigh a signal above the current maximum and a bar
 * wider than its track would break the plate's alignment.
 */
export function barWidth(contribution: number, total: number): number {
  if (total <= 0) return 0;
  const share = (Math.abs(contribution) / total) * 100;
  return Math.min(100, Math.max(0, Math.round(share)));
}
