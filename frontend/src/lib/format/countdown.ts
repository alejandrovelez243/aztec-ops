/**
 * Tone of a countdown readout. Maps 1:1 onto the semantic colour tokens
 * (`--color-go`, `--color-caution`, `--color-nogo`, `--color-legend-dim`); the caller binds it
 * through a `data-tone` attribute, never a literal colour (DESIGN.md, The Three Meanings Rule).
 */
export type CountdownTone = "go" | "caution" | "nogo" | "dim";

/**
 * A formatted countdown: the tabular readout text plus its semantic tone.
 */
export interface Countdown {
  /** Mono-font readout, e.g. `12d`, `0d`, `+3d OVERRUN`, or the named condition `NO DATE`. */
  text: string;
  tone: CountdownTone;
}

/** Days from `now` after which the countdown stops being `caution` and is simply `go`. */
const FINAL_WEEK_DAYS = 7;

const DAY_MS = 86_400_000;

/**
 * Formats a target date as a days-based countdown clock.
 *
 * Invariant: the clock crosses into overrun in the same format (`+Nd OVERRUN`, tone `nogo`)
 * rather than switching shape, and a missing date is the named condition `NO DATE` (tone
 * `dim`), never an empty cell (DESIGN.md, Do's). Inside the final week — including
 * exactly-today — the tone is `caution`; beyond it, `go`.
 *
 * Edge cases:
 *
 * - `null` target → `{ text: "NO DATE", tone: "dim" }`. `NO_TARGET_DATE` is a real risk flag
 *   on the board, so the cell names it instead of rendering blank.
 * - Exactly today → `0d` with tone `caution`: the deadline is inside its final week and the
 *   day is not over yet, so it is not an overrun.
 * - Negative distance (yesterday, last week) → `+Nd OVERRUN` with tone `nogo`, where `N` is
 *   the whole calendar days elapsed since the target.
 *
 * Distance is measured in calendar days on the UTC clock (the API sends `date` fields as
 * `YYYY-MM-DD`, which parse as UTC midnight), so DST transitions and the local time of day
 * never shift the readout by one.
 */
export function formatCountdown(
  targetDate: string | null,
  now?: Date,
): Countdown {
  if (targetDate === null) {
    return { text: "NO DATE", tone: "dim" };
  }
  const target = new Date(targetDate);
  const reference = now ?? new Date();
  const days = calendarDayDiff(target, reference);
  if (days < 0) {
    return { text: `+${Math.abs(days)}d OVERRUN`, tone: "nogo" };
  }
  if (days <= FINAL_WEEK_DAYS) {
    return { text: `${days}d`, tone: "caution" };
  }
  return { text: `${days}d`, tone: "go" };
}

/**
 * Whole calendar days from `from` to `to`, comparing both at UTC midnight.
 *
 * Floored after the midnight normalisation rather than on the raw millisecond distance, so
 * `23:59` against `00:01` the next day is one day, and a same-day pair is always zero.
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
