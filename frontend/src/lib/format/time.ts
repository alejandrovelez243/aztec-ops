/**
 * Instants as a person reads them: "hace 3 días", with "28 jul 2026, 09:05"
 * behind it.
 *
 * These two formatters were born inside the project timeline and now serve the
 * portfolio-wide feed as well, so they live in `lib/` — framework-free, no Astro
 * import — instead of under one view's directory. Two copies would eventually
 * disagree about the same instant, which is how the detail page and the feed
 * start telling a different story about one change.
 *
 * Only instants (`...Z`) belong here. Calendar dates (`YYYY-MM-DD`) are parsed
 * separately by the surfaces that render deadlines, because the native parser
 * reads them as UTC midnight and renders the day before west of Greenwich.
 */

const instantFormat = new Intl.DateTimeFormat("es", {
  day: "numeric",
  month: "short",
  year: "numeric",
  hour: "2-digit",
  minute: "2-digit",
});

const relativeFormat = new Intl.RelativeTimeFormat("es", { numeric: "auto" });

const MINUTE_S = 60;
const HOUR_S = 3_600;
const DAY_S = 86_400;
const MONTH_S = 2_592_000;
const YEAR_S = 31_536_000;

/**
 * Parses an ISO instant.
 *
 * @returns `null` for absent or unparseable input — absence is a fact the
 *   caller must render as such, never a silent "ahora".
 */
export function parseInstant(value: string | null | undefined): Date | null {
  if (value === null || value === undefined || value === "") return null;
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? null : parsed;
}

/** An instant as "28 jul 2026, 09:05" — the `title` behind every relative time. */
export function formatInstant(value: string | null | undefined): string | null {
  const parsed = parseInstant(value);
  return parsed === null ? null : instantFormat.format(parsed);
}

/**
 * A past instant as "hace 5 minutos" / "hace 3 días".
 *
 * Future instants read "en 2 días" — the same formatter, because a record
 * written under a clock skew must not silently render as the past.
 *
 * @param now - Injected so a whole server render shares one reference instant
 *   and two rows cannot straddle a minute boundary.
 */
export function relativeTime(
  value: string | null | undefined,
  now: Date = new Date(),
): string | null {
  const parsed = parseInstant(value);
  if (parsed === null) return null;
  const seconds = (parsed.getTime() - now.getTime()) / 1000;
  const magnitude = Math.abs(seconds);
  if (magnitude < MINUTE_S) {
    return relativeFormat.format(Math.round(seconds), "second");
  }
  if (magnitude < HOUR_S) {
    return relativeFormat.format(Math.round(seconds / MINUTE_S), "minute");
  }
  if (magnitude < DAY_S) {
    return relativeFormat.format(Math.round(seconds / HOUR_S), "hour");
  }
  if (magnitude < MONTH_S) {
    return relativeFormat.format(Math.round(seconds / DAY_S), "day");
  }
  if (magnitude < YEAR_S) {
    return relativeFormat.format(Math.round(seconds / MONTH_S), "month");
  }
  return relativeFormat.format(Math.round(seconds / YEAR_S), "year");
}
