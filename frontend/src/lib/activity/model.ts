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
  SYSTEM_ACTOR,
  SYSTEM_ACTOR_LABEL,
  verbLabel,
} from "./vocabulary";

/** One project as the feed needs it: enough to name a code and link to it. */
export interface ProjectRef {
  readonly code: string;
  readonly name: string;
}

/** One person as the feed needs it: their `accounts.User.code` and their name. */
export interface MemberRef {
  readonly alias: string;
  readonly label: string;
}

/**
 * The names the app already holds, so a record's code can be read as a thing.
 *
 * Projects are kept longest-code-first, which is what makes prefix resolution
 * correct rather than lucky: a task code `PRJ-1-T02` must resolve to `PRJ-1` and
 * never to a hypothetical `PRJ` that happens to sort earlier.
 */
export interface Directory {
  readonly projects: readonly ProjectRef[];
  readonly members: readonly MemberRef[];
}

/** Builds the lookup from the lists the page already fetched. */
export function buildDirectory(
  projects: readonly ProjectRef[],
  members: readonly MemberRef[],
): Directory {
  return {
    projects: [...projects].sort((a, b) => b.code.length - a.code.length),
    members: [...members],
  };
}

/** An empty directory: every subject then renders as its bare code. */
export const EMPTY_DIRECTORY: Directory = { projects: [], members: [] };

/**
 * What a record is about, resolved to something a person can read and open.
 *
 * The kind is taken from `entity_type` and **never guessed from the id**. That
 * was the defect: a member's `entity_id` is an `accounts.User.code`, and running
 * it through the project prefix match put a phantom project name and a project
 * link on a row about a person. A record says what kind of thing it concerns;
 * the frontend's job is to believe it.
 */
export type ActivitySubject =
  | {
      readonly kind: "project";
      /** The project the record belongs to, even when the id is a task's. */
      readonly code: string;
      readonly name: string;
      readonly href: string;
    }
  | { readonly kind: "member"; readonly alias: string; readonly label: string }
  | { readonly kind: "plain" };

/** The project a project- or task-scoped code belongs to. */
function findProject(
  entityId: string,
  directory: Directory,
): ProjectRef | null {
  for (const entry of directory.projects) {
    if (entityId === entry.code || entityId.startsWith(`${entry.code}-`)) {
      return entry;
    }
  }
  return null;
}

/**
 * Resolves one record's subject.
 *
 * A kind the directory cannot name — a blocker's numeric id, a role code, a
 * project archived out of the queue this page read — is `plain`, and the row
 * renders the bare business code. That is the honest answer: the trail outlives
 * the rows it describes, and inventing a name for something no longer on file
 * would be worse than showing the code somebody can search for.
 */
export function resolveSubject(
  entityType: string,
  entityId: string,
  directory: Directory,
): ActivitySubject {
  if (entityType === "member") {
    const member = directory.members.find((one) => one.alias === entityId);
    return {
      kind: "member",
      alias: entityId,
      label: member?.label ?? entityId,
    };
  }
  if (entityType === "project" || entityType === "task") {
    const project = findProject(entityId, directory);
    if (project === null) return { kind: "plain" };
    return {
      kind: "project",
      code: project.code,
      name: project.name,
      href: `/projects/${encodeURIComponent(project.code)}`,
    };
  }
  return { kind: "plain" };
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
  /** What the record is about, resolved from its declared kind. */
  readonly subject: ActivitySubject;
  /** The subject's name, or `""` when only its code is known. */
  readonly subjectLabel: string;
  /** Where the subject can be opened, or `null` when it has no page. */
  readonly subjectHref: string | null;
  readonly occurredAt: string;
  /** "hace 3 días"; falls back to the raw instant if it cannot be parsed. */
  readonly occurredLabel: string;
  /** "28 jul 2026, 09:05", shown as the title behind the relative time. */
  readonly occurredTitle: string;
  readonly correlationId: string;
}

/** The subject's readable name, or `""` when the directory could not name it. */
function labelOf(subject: ActivitySubject): string {
  if (subject.kind === "project") return subject.name;
  if (subject.kind === "member") return subject.label;
  return "";
}

/** Maps one record onto the row the feed renders. */
export function toRow(
  entry: ActivityEntry,
  directory: Directory,
  now: Date,
): ActivityRow {
  const subject = resolveSubject(entry.entity_type, entry.entity_id, directory);
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
    subject,
    subjectLabel: labelOf(subject),
    subjectHref: subject.kind === "project" ? subject.href : null,
    occurredAt: entry.occurred_at,
    occurredLabel: relativeTime(entry.occurred_at, now) ?? entry.occurred_at,
    occurredTitle: formatInstant(entry.occurred_at) ?? entry.occurred_at,
    correlationId: entry.correlation_id,
  };
}

/** Maps a page of records, newest first, preserving the server's order. */
export function toRows(
  entries: readonly ActivityEntry[],
  directory: Directory,
  now: Date,
): readonly ActivityRow[] {
  return entries.map((entry) => toRow(entry, directory, now));
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
