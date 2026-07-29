/**
 * Calendar arithmetic and range selection — pure, DOM-free, dependency-free.
 *
 * Why this is a module and not code inside the picker: the same answers are
 * needed by the server render (the trigger's label), by the panel (which days a
 * month holds, which are inside the range) and by the keyboard (what "one week
 * earlier" means at a month boundary). Written three times they would eventually
 * disagree, and a calendar that disagrees with itself is a calendar nobody can
 * trust. It is placed here, beside `lib/activity/filters.ts`'s `pagerModel`, for
 * the same reason: it is testable in isolation because it touches nothing.
 *
 * **A day is not an instant.** Every value here is a civil date — year, month,
 * day — and never a `Date`. `new Date("2026-08-12")` is UTC midnight, which is
 * the 11th anywhere west of Greenwich; a range picked at 23:00 local would then
 * mean different days than the ones the grid displayed. Arithmetic goes through
 * `Date.UTC` and comes straight back out as civil fields, so no value ever
 * carries a timezone it could drift by, and no `Date` is ever mutated.
 *
 * **Month names and weekday initials come from `Intl`**, formatted in UTC to
 * match the civil dates above. A hand-written Spanish month table would be a
 * fourth source of truth for something the platform already knows.
 */

/** Locale of the calendar. `es-ES` is also what makes the week start on Monday. */
const CALENDAR_LOCALE = "es-ES";

const DAY_MS = 86_400_000;

/** Weeks rendered per month; fixed so the panel never changes height. */
const WEEKS_PER_MONTH = 6;

const DAYS_PER_WEEK = 7;

/** A Monday, used only to name the weekday columns in the reader's language. */
const REFERENCE_MONDAY = Date.UTC(2024, 0, 1);

/** One calendar day, with no time and no zone. */
export interface CivilDate {
  readonly year: number;
  /** 1–12, unlike `Date`'s 0–11: an off-by-one here is a whole wrong month. */
  readonly month: number;
  readonly day: number;
}

/** The persisted value of a range. Either end may be absent and mean "open". */
export interface RangeValue {
  readonly since: CivilDate | null;
  readonly until: CivilDate | null;
}

/** Nothing selected. */
export const EMPTY_RANGE: RangeValue = { since: null, until: null };

/**
 * A range as the picker holds it while the operator works.
 *
 * `picking` carries the anchor the next click will close against. The end of a
 * range is only ever written in the branch where the clicked day is not before
 * that anchor, which is what makes an inverted range **unrepresentable** rather
 * than validated after the fact: there is no code path that can produce
 * `until < since`.
 */
export type RangeDraft =
  | { readonly kind: "idle"; readonly value: RangeValue }
  | {
      readonly kind: "picking";
      readonly anchor: CivilDate;
      readonly value: RangeValue;
    };

const monthYearFormat = new Intl.DateTimeFormat(CALENDAR_LOCALE, {
  month: "long",
  year: "numeric",
  timeZone: "UTC",
});

const monthNameFormat = new Intl.DateTimeFormat(CALENDAR_LOCALE, {
  month: "short",
  timeZone: "UTC",
});

const dayLongFormat = new Intl.DateTimeFormat(CALENDAR_LOCALE, {
  weekday: "long",
  day: "numeric",
  month: "long",
  year: "numeric",
  timeZone: "UTC",
});

const dayShortFormat = new Intl.DateTimeFormat(CALENDAR_LOCALE, {
  day: "numeric",
  month: "short",
  timeZone: "UTC",
});

const dayShortWithYearFormat = new Intl.DateTimeFormat(CALENDAR_LOCALE, {
  day: "numeric",
  month: "short",
  year: "numeric",
  timeZone: "UTC",
});

const weekdayNarrowFormat = new Intl.DateTimeFormat(CALENDAR_LOCALE, {
  weekday: "narrow",
  timeZone: "UTC",
});

const weekdayLongFormat = new Intl.DateTimeFormat(CALENDAR_LOCALE, {
  weekday: "long",
  timeZone: "UTC",
});

/**
 * The words the trigger says when one end or both are open.
 *
 * Spanish, like every other string a person reads (CLAUDE.md §Language). They
 * live beside the formatter that needs them rather than in a view's copy file,
 * because `formatRangeLabel` is the only thing that can decide which of the four
 * shapes a range has.
 */
const RANGE_COPY = {
  any: "Todo",
  fromOnly: "desde",
  untilOnly: "hasta",
  separator: "–",
} as const;

// --- Conversion --------------------------------------------------------------

function toEpoch(date: CivilDate): number {
  return Date.UTC(date.year, date.month - 1, date.day);
}

function fromEpoch(ms: number): CivilDate {
  const parsed = new Date(ms);
  return {
    year: parsed.getUTCFullYear(),
    month: parsed.getUTCMonth() + 1,
    day: parsed.getUTCDate(),
  };
}

function pad(value: number, width: number): string {
  return String(Math.abs(value)).padStart(width, "0");
}

/** `{2026, 8, 4}` → `"2026-08-04"`, the shape the URL and the API both use. */
export function toISODay(date: CivilDate): string {
  return `${pad(date.year, 4)}-${pad(date.month, 2)}-${pad(date.day, 2)}`;
}

/**
 * Parses `YYYY-MM-DD`.
 *
 * Strict by round-trip rather than by range checks: `2026-02-31` normalizes to
 * March 3rd, and silently accepting it would make the grid highlight a day the
 * operator never chose. Anything that does not survive the round trip is `null`.
 */
export function parseISODay(
  value: string | null | undefined,
): CivilDate | null {
  if (value === null || value === undefined) return null;
  const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(value.trim());
  if (match === null) return null;
  const [, year, month, day] = match;
  if (year === undefined || month === undefined || day === undefined) {
    return null;
  }
  const candidate: CivilDate = {
    year: Number(year),
    month: Number(month),
    day: Number(day),
  };
  return toISODay(fromEpoch(toEpoch(candidate))) === toISODay(candidate)
    ? candidate
    : null;
}

/**
 * Parses what a person types: `dd/mm/aaaa`, with `/`, `-` or `.` as separators.
 *
 * The typed path is not a convenience, it is the accessible path: a screen
 * reader user must be able to state a date instead of being walked through a
 * grid of forty-two buttons.
 */
export function parseTypedDay(text: string): CivilDate | null {
  const match = /^(\d{1,2})[/\-.](\d{1,2})[/\-.](\d{4})$/.exec(text.trim());
  if (match === null) return null;
  const [, day, month, year] = match;
  if (day === undefined || month === undefined || year === undefined) {
    return null;
  }
  return parseISODay(
    `${pad(Number(year), 4)}-${pad(Number(month), 2)}-${pad(Number(day), 2)}`,
  );
}

/** `{2026, 8, 4}` → `"04/08/2026"`, the shape the typed inputs show. */
export function formatTypedDay(date: CivilDate | null): string {
  if (date === null) return "";
  return `${pad(date.day, 2)}/${pad(date.month, 2)}/${pad(date.year, 4)}`;
}

/** The operator's own today, read from their clock in their own calendar. */
export function todayCivil(now: Date = new Date()): CivilDate {
  return {
    year: now.getFullYear(),
    month: now.getMonth() + 1,
    day: now.getDate(),
  };
}

// --- Arithmetic --------------------------------------------------------------

/** Negative when `left` is earlier; zero when they are the same day. */
export function compareDays(left: CivilDate, right: CivilDate): number {
  return toEpoch(left) - toEpoch(right);
}

export function isSameDay(left: CivilDate, right: CivilDate): boolean {
  return (
    left.year === right.year &&
    left.month === right.month &&
    left.day === right.day
  );
}

/** How many days a month holds; day 0 of the next month is the last of this one. */
export function daysInMonth(year: number, month: number): number {
  return new Date(Date.UTC(year, month, 0)).getUTCDate();
}

export function addDays(date: CivilDate, days: number): CivilDate {
  return fromEpoch(toEpoch(date) + days * DAY_MS);
}

/**
 * Adds months, clamping the day.
 *
 * 31 January plus one month is 28 February, not 3 March: paging a calendar must
 * land on the month the operator asked for, and `Date`'s own overflow would skip
 * it entirely.
 */
export function addMonths(date: CivilDate, months: number): CivilDate {
  const total = date.year * 12 + (date.month - 1) + months;
  const year = Math.floor(total / 12);
  const month = total - year * 12 + 1;
  return { year, month, day: Math.min(date.day, daysInMonth(year, month)) };
}

/** 0 for Monday … 6 for Sunday — the week this locale actually starts on. */
export function weekdayIndex(date: CivilDate): number {
  return (new Date(toEpoch(date)).getUTCDay() + 6) % 7;
}

export function startOfWeek(date: CivilDate): CivilDate {
  return addDays(date, -weekdayIndex(date));
}

export function endOfWeek(date: CivilDate): CivilDate {
  return addDays(date, 6 - weekdayIndex(date));
}

/** Whether `day` falls inside `[from, to]`; an absent end leaves that side open. */
export function isWithin(
  day: CivilDate,
  from: CivilDate | null,
  to: CivilDate | null,
): boolean {
  if (from !== null && compareDays(day, from) < 0) return false;
  if (to !== null && compareDays(day, to) > 0) return false;
  return from !== null || to !== null;
}

// --- The month grid ----------------------------------------------------------

/** One cell of the grid: a day, and whether it belongs to the month on screen. */
export interface MonthCell {
  readonly date: CivilDate;
  readonly inMonth: boolean;
}

/**
 * The six weeks a month is drawn in, Monday first.
 *
 * Always six, including the leading and trailing days of the neighbouring
 * months: a grid that grows and shrinks between five and six rows makes the
 * panel jump under the pointer while the operator is aiming at a date.
 */
export function monthMatrix(
  year: number,
  month: number,
): readonly (readonly MonthCell[])[] {
  const first = startOfWeek({ year, month, day: 1 });
  const weeks: MonthCell[][] = [];
  for (let week = 0; week < WEEKS_PER_MONTH; week += 1) {
    const row: MonthCell[] = [];
    for (let index = 0; index < DAYS_PER_WEEK; index += 1) {
      const date = addDays(first, week * DAYS_PER_WEEK + index);
      row.push({ date, inMonth: date.year === year && date.month === month });
    }
    weeks.push(row);
  }
  return weeks;
}

// --- Selection ---------------------------------------------------------------

/**
 * Applies one click to the draft.
 *
 * Two outcomes only, and that is the whole design: clicking with no anchor (or
 * clicking before the current anchor) **restarts** the range at that day, and
 * clicking on or after the anchor **closes** it. There is no third branch in
 * which an end lands before its start, so an inverted range cannot be built and
 * therefore never needs to be rejected.
 */
export function pickDay(draft: RangeDraft, day: CivilDate): RangeDraft {
  if (draft.kind === "idle" || compareDays(day, draft.anchor) < 0) {
    return { kind: "picking", anchor: day, value: { since: day, until: null } };
  }
  return { kind: "idle", value: { since: draft.anchor, until: day } };
}

/**
 * Sets the start from the typed field, keeping the invariant.
 *
 * A start after the current end does not invert the range: it opens a new one,
 * exactly as clicking a day before the anchor does. Clearing the field opens
 * that side rather than clearing both.
 */
export function setSince(
  value: RangeValue,
  since: CivilDate | null,
): RangeValue {
  if (since === null) return { since: null, until: value.until };
  if (value.until !== null && compareDays(since, value.until) > 0) {
    return { since, until: null };
  }
  return { since, until: value.until };
}

/** Sets the end from the typed field; an end before the start opens the start. */
export function setUntil(
  value: RangeValue,
  until: CivilDate | null,
): RangeValue {
  if (until === null) return { since: value.since, until: null };
  if (value.since !== null && compareDays(until, value.since) < 0) {
    return { since: null, until };
  }
  return { since: value.since, until };
}

/**
 * The span to paint while the operator is choosing the end.
 *
 * The preview is the anchor to the hovered day, in whichever order they fall, so
 * hovering backwards shows what a click would actually do — restart — instead of
 * an inverted band the range could never hold.
 */
export function previewRange(
  draft: RangeDraft,
  hovered: CivilDate | null,
): RangeValue {
  if (draft.kind === "idle" || hovered === null) return draft.value;
  return compareDays(hovered, draft.anchor) < 0
    ? { since: hovered, until: draft.anchor }
    : { since: draft.anchor, until: hovered };
}

// --- Labels ------------------------------------------------------------------

function asDate(date: CivilDate): Date {
  return new Date(toEpoch(date));
}

/**
 * Capitalizes the first letter and leaves the rest alone.
 *
 * Spanish does not capitalize months, and CSS `text-transform: capitalize`
 * capitalizes every *word*, which turns `julio de 2026` into `Julio De 2026`.
 * The sentence starts with a capital because it is a heading; the preposition in
 * the middle of it does not.
 */
function sentenceCase(text: string): string {
  return text.charAt(0).toUpperCase() + text.slice(1);
}

/** "Julio de 2026", for the panel's header. */
export function monthYearLabel(year: number, month: number): string {
  return sentenceCase(
    monthYearFormat.format(new Date(Date.UTC(year, month - 1, 1))),
  );
}

/** "Jul", for the twelve buttons of the month jump. */
export function monthShortLabel(month: number): string {
  return sentenceCase(
    monthNameFormat.format(new Date(Date.UTC(2024, month - 1, 1))),
  );
}

/** "martes, 28 de julio de 2026" — the accessible name of one day cell. */
export function dayLongLabel(date: CivilDate): string {
  return dayLongFormat.format(asDate(date));
}

/** The seven column initials, Monday first, with their full names for a11y. */
export function weekdayHeadings(): readonly {
  readonly initial: string;
  readonly name: string;
}[] {
  return Array.from({ length: DAYS_PER_WEEK }, (_, index) => {
    const day = new Date(REFERENCE_MONDAY + index * DAY_MS);
    return {
      initial: weekdayNarrowFormat.format(day),
      name: weekdayLongFormat.format(day),
    };
  });
}

/**
 * How the trigger reads: "28 jul – 4 ago", "desde 1 jul", "hasta 4 ago", "Todo".
 *
 * The year is spelled out only when some end falls outside the year being lived
 * in — "28 jul 2026 – 4 ago 2026" is noise on a board whose whole subject is
 * this quarter, while "4 ago" alone would be a lie about a range in 2024. Both
 * ends then carry it, so the two halves of one label cannot be read in different
 * calendars.
 */
export function formatRangeLabel(
  value: RangeValue,
  now: Date = new Date(),
): string {
  const { since, until } = value;
  if (since === null && until === null) return RANGE_COPY.any;

  const currentYear = todayCivil(now).year;
  const withYear =
    (since !== null && since.year !== currentYear) ||
    (until !== null && until.year !== currentYear);
  const format = withYear ? dayShortWithYearFormat : dayShortFormat;

  if (until === null && since !== null) {
    return `${RANGE_COPY.fromOnly} ${format.format(asDate(since))}`;
  }
  if (since === null && until !== null) {
    return `${RANGE_COPY.untilOnly} ${format.format(asDate(until))}`;
  }
  if (since === null || until === null) return RANGE_COPY.any;
  return `${format.format(asDate(since))} ${RANGE_COPY.separator} ${format.format(asDate(until))}`;
}

/** The range spelled out for the live region when a selection completes. */
export function announceRange(value: RangeValue): string {
  const { since, until } = value;
  if (since !== null && until !== null) {
    return `${dayLongLabel(since)} ${RANGE_COPY.separator} ${dayLongLabel(until)}`;
  }
  if (since !== null) return `${RANGE_COPY.fromOnly} ${dayLongLabel(since)}`;
  if (until !== null) return `${RANGE_COPY.untilOnly} ${dayLongLabel(until)}`;
  return RANGE_COPY.any;
}
