/**
 * The presentation model of the portfolio-wide feed: one flat row per record,
 * and the grouping that turns correlated records back into the single decision
 * they were.
 *
 * Why a model rather than handing `ActivityEntry` to the components: the feed
 * renders on the server for the first paint and again in the browser when the
 * stream says something moved, and both paths must produce the same words. A
 * component reading the wire shape directly would put that decision in two
 * places and let them drift.
 *
 * Framework-free and pure — no Astro import, no DOM — so the same function fills
 * a server-rendered row and a cloned template.
 */

import type { ActivityEntry } from "../api/domain";
import { formatInstant, relativeTime } from "../format/time";
import {
  activityChange,
  entityTypeLabel,
  originBadge,
  originTone,
  policyExplanation,
  verbLabel,
} from "./vocabulary";

/** One project as the feed needs it: enough to name a code and link to it. */
export interface ProjectRef {
  readonly code: string;
  readonly name: string;
}

/**
 * Business codes the app already knows, longest first.
 *
 * Longest-first is what makes prefix resolution correct rather than lucky: a
 * task code `PRJ-1-T02` must resolve to `PRJ-1` and never to a hypothetical
 * `PRJ` that happens to sort earlier.
 */
export interface ProjectIndex {
  readonly entries: readonly ProjectRef[];
}

/** Builds the lookup from whatever project list the page already fetched. */
export function buildProjectIndex(
  projects: readonly ProjectRef[],
): ProjectIndex {
  return {
    entries: [...projects].sort((a, b) => b.code.length - a.code.length),
  };
}

/**
 * Resolves a record's subject to the project it belongs to.
 *
 * `entity_id` is a business code, and a task's code carries its project's as a
 * prefix (`PRJ-01-T02`), so one index answers for both. A blocker names itself
 * with an id that belongs to no project code, and that is a legitimate `null`:
 * the row then renders its subject without a link rather than guessing.
 */
export function resolveProject(
  entityId: string,
  index: ProjectIndex,
): ProjectRef | null {
  for (const entry of index.entries) {
    if (entityId === entry.code || entityId.startsWith(`${entry.code}-`)) {
      return entry;
    }
  }
  return null;
}

/** One record, with every string it renders already decided. */
export interface ActivityRow {
  readonly id: number;
  readonly verb: string;
  readonly verbLabel: string;
  readonly actor: string;
  readonly origin: string;
  readonly originLabel: string;
  readonly originTone: string;
  /** `MANUAL` rows carry a mandatory reason and are read, not skimmed. */
  readonly isManual: boolean;
  /** "antes → después", or `""` when the record has neither side. */
  readonly change: string;
  readonly reason: string;
  /** The engine's own note on a recomputation, or `""`. */
  readonly explanation: string;
  readonly entityType: string;
  readonly entityTypeLabel: string;
  readonly entityId: string;
  /** `null` when the subject resolves to no project (a blocker id). */
  readonly projectCode: string | null;
  readonly projectName: string | null;
  readonly occurredAt: string;
  /** "hace 3 días"; falls back to the raw instant if it cannot be parsed. */
  readonly occurredLabel: string;
  /** "28 jul 2026, 09:05", shown as the title behind the relative time. */
  readonly occurredTitle: string;
  readonly correlationId: string;
}

/** The actor string the platform signs its own records with. */
const SYSTEM_ACTOR = "system";

/** How the platform's own signature reads on screen. */
const SYSTEM_ACTOR_LABEL = "Sistema";

/** Maps one record onto the row the feed renders. */
export function toRow(
  entry: ActivityEntry,
  index: ProjectIndex,
  now: Date,
): ActivityRow {
  const project = resolveProject(entry.entity_id, index);
  const origin = entry.origin;
  return {
    id: entry.id,
    verb: entry.verb,
    verbLabel: verbLabel(entry.verb),
    actor: entry.actor === SYSTEM_ACTOR ? SYSTEM_ACTOR_LABEL : entry.actor,
    origin,
    originLabel: originBadge(origin),
    originTone: originTone(origin),
    isManual: origin === "MANUAL",
    change: activityChange(entry),
    reason: entry.reason,
    explanation: policyExplanation(entry.metadata),
    entityType: entry.entity_type,
    entityTypeLabel: entityTypeLabel(entry.entity_type),
    entityId: entry.entity_id,
    projectCode: project?.code ?? null,
    projectName: project?.name ?? null,
    occurredAt: entry.occurred_at,
    occurredLabel: relativeTime(entry.occurred_at, now) ?? entry.occurred_at,
    occurredTitle: formatInstant(entry.occurred_at) ?? entry.occurred_at,
    correlationId: entry.correlation_id,
  };
}

/** Maps a page of records, newest first, preserving the server's order. */
export function toRows(
  entries: readonly ActivityEntry[],
  index: ProjectIndex,
  now: Date,
): readonly ActivityRow[] {
  return entries.map((entry) => toRow(entry, index, now));
}

/**
 * A run of records that were written as one decision.
 *
 * `isDecision` is true only when the run holds more than one record: a lone row
 * sharing nobody's correlation id is just a change, and framing it as a decision
 * would make the grouping meaningless where it matters — the reprioritization
 * that reads "A bajó para que B subiera".
 */
export interface DecisionGroup {
  readonly correlationId: string;
  readonly rows: readonly ActivityRow[];
  readonly isDecision: boolean;
}

/**
 * Groups consecutive rows sharing a `correlation_id`.
 *
 * Consecutive, not global: the feed is ordered by time and correlated records
 * are written in one transaction, so they arrive adjacent. Collecting
 * non-adjacent matches would reorder the narrative to satisfy a grouping, and a
 * decision split across a page boundary is honestly shown as split — the reader
 * follows the "ver la decisión completa" link to see all of it.
 */
export function groupByCorrelation(
  rows: readonly ActivityRow[],
): readonly DecisionGroup[] {
  const groups: DecisionGroup[] = [];
  let current: ActivityRow[] = [];

  const flush = (): void => {
    const first = current[0];
    if (first === undefined) return;
    groups.push({
      correlationId: first.correlationId,
      rows: current,
      isDecision: current.length > 1,
    });
    current = [];
  };

  for (const row of rows) {
    const open = current[0];
    if (open !== undefined && open.correlationId !== row.correlationId) flush();
    current.push(row);
  }
  flush();
  return groups;
}
