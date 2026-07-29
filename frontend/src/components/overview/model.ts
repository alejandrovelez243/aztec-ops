/**
 * Pure derivation for the Resumen surface: a queue page becomes the rows the
 * three density zones render.
 *
 * Framework-free on purpose (no Astro import, `docs/standards/FRONTEND.md` §3):
 * the same functions run in page frontmatter for the first paint and in the
 * live island after an SSE reshuffle, so the server render and the live render
 * can never disagree about what a row *is*.
 *
 * Nothing here knows a workflow state code, a priority code or a risk code.
 * Zones are rank bands over the server's own ordering (`-score`, then code),
 * which is where risk already lives: the persisted breakdown weighs blockage,
 * overdue work and staleness before this module sees a number.
 */

import { avatarHue, initials } from "../../lib/auth/session";
import type { QueueItem, ScoreBreakdownEntry } from "../../lib/api/domain";

/** The three density tiers of comp C. Rank decides the tier, nothing else. */
export type ZoneKey = "today" | "week" | "radar";

/**
 * How many queue rows the Resumen reads.
 *
 * One constant for both reads — the page's first paint and the island's
 * client-side re-read — because a radar built from 50 rows on the server and 20
 * on the client would silently drop projects on reconnect.
 */
export const QUEUE_PAGE_SIZE = 50;

/** Last rank that still belongs to "Atender hoy". */
export const ZONE_TODAY_MAX_RANK = 3;

/** Last rank that still belongs to "Esta semana"; everything after is radar. */
export const ZONE_WEEK_MAX_RANK = 10;

/**
 * Widest set of bars any tier shows.
 *
 * Every card carries this many and the zone's stylesheet hides the surplus
 * (3 in "Atender hoy", 2 in "Esta semana", 1 in "Radar"): density *is* depth of
 * argument, and a card that travels between zones must not be rebuilt to change
 * how much of its breakdown is visible.
 */
const MAX_BARS = 3;

const DAY_MS = 86_400_000;

/**
 * The due state of one project, as a closed set of cases.
 *
 * A union rather than `{ text, isOverdue, isMissing }`: "overdue *and* missing"
 * is not a state a project can be in, and the copy for each case is written
 * once against its own arm instead of being reassembled from booleans.
 */
export type DueState =
  | { kind: "overdue"; days: number }
  | { kind: "today" }
  | { kind: "ahead"; days: number }
  | { kind: "missing" };

/** One line of the persisted argument, ready to draw as a labelled bar. */
export interface BreakdownBar {
  /** Spanish label for the signal; an unregistered code renders humanized. */
  readonly label: string;
  /** `raw * weight * 100` exactly as the server computed it — never recomputed. */
  readonly contribution: number;
  /** 0–1 against the row's own leading signal, so bars compare inside a card. */
  readonly share: number;
}

/** The owner of a project, resolved to what an avatar disk needs. */
export interface OwnerBadge {
  readonly alias: string;
  readonly label: string;
  readonly initials: string;
  /** Deterministic hue so one person keeps one colour across every surface. */
  readonly hue: number;
}

/** A workflow state as the card renders it: label and colour from the API. */
export interface StateBadge {
  readonly code: string;
  readonly label: string;
  readonly category: string;
  readonly color: string | null;
}

/**
 * One queue row, resolved into exactly what the card markup draws.
 *
 * `score` is always the value the engine computed. An `override` never
 * overwrites it — it travels beside it, so the evidence that a human argued
 * with the ranking survives the render (`docs/API.md`, `QueueItemView`).
 */
export interface OverviewRow {
  /** Continuous position in the whole queue, 1-based; zones never restart it. */
  readonly rank: number;
  readonly code: string;
  readonly name: string;
  readonly clientAlias: string;
  readonly owner: OwnerBadge | null;
  readonly state: StateBadge;
  readonly score: number;
  /** Empty when the engine persisted no breakdown; the card then names the absence. */
  readonly bars: readonly BreakdownBar[];
  readonly due: DueState;
  readonly hasNextStep: boolean;
  /** The manual decision beside the computed score, with its mandatory reason. */
  readonly overrideReason: string | null;
  readonly openBlockers: number;
  /** The `updated_at` the row was rendered with; older envelopes are dropped. */
  readonly updatedAt: string;
}

/**
 * Which zone a rank belongs to.
 *
 * Thresholds are named constants over the server's ordering, never a list of
 * state codes: a workflow state added from the admin changes nothing here.
 */
export function zoneOfRank(rank: number): ZoneKey {
  if (rank <= ZONE_TODAY_MAX_RANK) return "today";
  if (rank <= ZONE_WEEK_MAX_RANK) return "week";
  return "radar";
}

/**
 * Resolves a queue page into overview rows, keeping the server's order.
 *
 * Rank is the index in the response, so it is the ordering the API decided
 * (`-priority_score`, then `project_code`) and not a client re-sort that could
 * disagree with the page the operator asked for.
 *
 * @param items - The page's rows, in the order the API returned them.
 * @param now - Reference instant for the due readouts; injected so the server
 *   render and a later client render can be compared in tests.
 */
export function toRows(
  items: readonly QueueItem[],
  now: Date,
): readonly OverviewRow[] {
  return items.map((item, index) => toRow(item, index + 1, now));
}

/** Resolves one queue row. See {@link toRows}. */
export function toRow(
  item: QueueItem,
  rank: number,
  now: Date,
): OverviewRow {
  const owner = item.owner ?? null;
  return {
    rank,
    code: item.code,
    name: item.name,
    clientAlias: item.client_alias,
    owner:
      owner === null
        ? null
        : {
            alias: owner.alias,
            label: owner.label,
            initials: initials(owner.label),
            hue: avatarHue(owner.alias),
          },
    state: {
      code: item.state.code,
      label: item.state.label,
      category: item.state.category,
      color: item.state.color ?? null,
    },
    score: item.score.value,
    bars: toBars(item.score.breakdown),
    due: toDueState(item.target_date ?? null, now),
    hasNextStep: (item.next_step ?? "").trim() !== "",
    overrideReason: item.override?.reason ?? null,
    openBlockers: item.open_blockers,
    updatedAt: item.updated_at,
  };
}

/**
 * Takes the leading lines of a persisted breakdown and sizes them against each
 * other.
 *
 * The API sends `breakdown` sorted by `contribution` descending and that order
 * is the writer's; it is preserved rather than re-sorted, so a policy that emits
 * ties keeps a stable presentation. `share` is relative to the row's own leading
 * signal — the bars answer "what is carrying this score", not "how does this
 * project compare to another one", which is what the rank already says.
 *
 * A breakdown whose contributions are all zero or negative yields zero-width
 * bars rather than a division by zero.
 */
export function toBars(
  breakdown: readonly ScoreBreakdownEntry[],
): readonly BreakdownBar[] {
  const leading = breakdown.slice(0, MAX_BARS);
  let peak = 0;
  for (const entry of leading) {
    if (entry.contribution > peak) peak = entry.contribution;
  }
  return leading.map((entry) => ({
    label: signalLabel(entry.code),
    contribution: entry.contribution,
    share: peak <= 0 ? 0 : clamp01(entry.contribution / peak),
  }));
}

/**
 * Classifies a target date against today.
 *
 * `null` is the `NO_TARGET_DATE` signal itself and becomes `missing`, never a
 * substituted date: replacing an absent deadline with today's would erase a
 * real risk (`docs/API.md`, `QueueItemView`).
 *
 * Distance is whole calendar days on the UTC clock — the API sends `date`
 * fields as `YYYY-MM-DD`, which parse as UTC midnight — so a DST change or the
 * operator's local time of day never shifts the readout by one.
 */
export function toDueState(targetDate: string | null, now: Date): DueState {
  if (targetDate === null || targetDate === "") return { kind: "missing" };
  const parsed = new Date(targetDate);
  if (Number.isNaN(parsed.getTime())) return { kind: "missing" };
  const days = calendarDayDiff(parsed, now);
  if (days < 0) return { kind: "overdue", days: Math.abs(days) };
  if (days === 0) return { kind: "today" };
  return { kind: "ahead", days };
}

/**
 * Spanish label for one priority signal.
 *
 * A translation table, never a whitelist: a signal registered on the backend
 * tomorrow (CLAUDE.md rule 8) still renders — humanized from its own code —
 * instead of vanishing from the card. A dropped signal is an argument nobody
 * can read.
 */
export function signalLabel(code: string): string {
  return SIGNAL_LABELS[code] ?? humanize(code);
}

/**
 * The six signals the current policy registers. Extending this file is how a
 * new signal *gets translated*, not how it gets permission to render.
 */
const SIGNAL_LABELS: Readonly<Record<string, string>> = {
  deadline_pressure: "Presión de fecha",
  overdue_work: "Trabajo vencido",
  criticality: "Criticidad",
  business_value: "Valor de negocio",
  blockage: "Bloqueos",
  staleness: "Sin movimiento",
};

/** `deadline_pressure` → `Deadline pressure`; the last-resort readable form. */
function humanize(code: string): string {
  const words = code.replace(/[_-]+/g, " ").trim();
  if (words === "") return code;
  return words.charAt(0).toUpperCase() + words.slice(1);
}

function clamp01(value: number): number {
  if (value < 0) return 0;
  if (value > 1) return 1;
  return value;
}

/** Whole calendar days from `from` to `to`, both taken at UTC midnight. */
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
