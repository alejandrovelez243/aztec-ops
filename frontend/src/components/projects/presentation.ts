/**
 * The presentation model of one project row, and the facets the toolbar builds
 * from a page of them.
 *
 * Why a model rather than passing `QueueItem` straight into the card: the card
 * and the table row would then both depend on the generated wire shape, and a
 * renamed field would edit two components instead of one mapper. This type is
 * the contract the projects surface renders; `toPresentation` is the only place
 * that knows what the API calls each value.
 *
 * It is deliberately flat and pre-derived — `searchKey` is folded once here
 * rather than recomputed inside a keystroke handler that runs over every row.
 */

import { activityChange } from "../../lib/activity/vocabulary";
import type {
  Blocker,
  Override,
  ProjectDetail,
  QueueItem,
  ScoreBreakdownEntry,
} from "../../lib/api/domain";
import {
  formatContribution,
  formatInstant,
  formatScore,
  relativeTime,
} from "./format";
import { signalLabel } from "./messages";
import { categoryLabel } from "./tone";

/**
 * "antes → después" is the trail's own phrasing and is shared with the
 * portfolio-wide feed (`lib/activity/vocabulary.ts`); re-exported so the
 * timeline's call sites keep one import.
 */
export { activityChange };

/**
 * One line of the argument behind a score, phrased for a list surface.
 *
 * The engine persists the whole document that defends a ranking; a card has no
 * room to draw it, but it may not print the number without it either (PRODUCT
 * principle 1, CLAUDE.md rule 7). This is that document reduced to what fits in
 * a tooltip and an accessible name.
 */
export interface ScoreReason {
  /** The signal's Spanish name, as the engine persisted it; the code if it shipped none. */
  readonly label: string;
  /** The contribution in points, signed, so a penalty reads as one. */
  readonly points: string;
  /** The engine's own sentence naming the fact the signal read. */
  readonly reason: string;
}

/** One project as every list surface renders it. */
export interface ProjectPresentation {
  readonly code: string;
  readonly name: string;
  readonly clientAlias: string;
  readonly ownerAlias: string | null;
  readonly ownerLabel: string | null;
  readonly stateLabel: string;
  readonly stateCategory: string;
  readonly stateColor: string | null;
  readonly engagementCode: string;
  readonly engagementLabel: string;
  readonly engagementColor: string | null;
  readonly healthCode: string;
  readonly healthLabel: string;
  readonly targetDate: string | null;
  readonly nextStep: string | null;
  readonly score: number;
  /**
   * The argument behind `score`, in the engine's order (contribution
   * descending). Empty means the recalculator has not run on this project yet,
   * which the surfaces say in words rather than leaving the figure undefended.
   */
  readonly scoreReasons: readonly ScoreReason[];
  /** A forced rank is shown beside the computed score, never instead of it. */
  readonly hasOverride: boolean;
  readonly riskCount: number;
  readonly openBlockers: number;
  readonly updatedAt: string;
  /** Lowercased `code + name + client`, matched by the toolbar's search. */
  readonly searchKey: string;
}

/** One value of a facet, with how many rows carry it. */
export interface Facet {
  readonly value: string;
  readonly label: string;
  readonly count: number;
}

/** Everything the filter pills need, built from the rows actually fetched. */
export interface Facets {
  readonly categories: readonly Facet[];
  readonly engagements: readonly Facet[];
  /** Rows whose derived health is not `HEALTHY`; the "en riesgo" pill. */
  readonly atRisk: number;
}

/**
 * Maps one queue row onto its presentation.
 *
 * Absent values stay `null` and are rendered as named absences by the
 * components (Present-Absence Rule); none of them is defaulted here, because a
 * substituted date is a risk the operator would never see again.
 */
export function toPresentation(item: QueueItem): ProjectPresentation {
  return {
    code: item.code,
    name: item.name,
    clientAlias: item.client_alias,
    ownerAlias: item.owner?.alias ?? null,
    ownerLabel: item.owner?.label ?? null,
    stateLabel: item.state.label,
    stateCategory: item.state.category,
    stateColor: item.state.color ?? null,
    engagementCode: item.engagement_type.code,
    engagementLabel: item.engagement_type.label,
    engagementColor: item.engagement_type.color ?? null,
    healthCode: item.health.code,
    healthLabel: item.health.label,
    targetDate: item.target_date ?? null,
    nextStep: item.next_step ?? null,
    score: item.score.value,
    scoreReasons: toScoreReasons(item.score.breakdown),
    hasOverride: item.override !== null && item.override !== undefined,
    riskCount: item.risk_flags.length,
    openBlockers: item.open_blockers,
    updatedAt: item.updated_at,
    searchKey: `${item.code} ${item.name} ${item.client_alias}`.toLowerCase(),
  };
}

/**
 * Reduces a persisted breakdown to the lines a list surface can carry.
 *
 * Every entry survives, in the order the engine wrote them: dropping the tail
 * would leave a card claiming that the signals it kept are the whole argument,
 * and a contribution silently removed is an argument nobody can audit. The
 * labels and the sentences both ship from the server, so a seventh signal
 * registered on the backend appears here with no frontend change (CLAUDE.md
 * rule 8).
 */
export function toScoreReasons(
  breakdown: readonly ScoreBreakdownEntry[],
): readonly ScoreReason[] {
  return breakdown.map((entry) => ({
    label: signalLabel(entry),
    points: formatContribution(entry.contribution),
    reason: entry.reason,
  }));
}

/** What a score with no argument behind it says instead of a bare figure. */
const NO_BREAKDOWN = "el motor todavía no registró señales para este puntaje";

/** Heading both phrasings share, so the two cannot state the number differently. */
function scoreHead(score: number): string {
  return `Prioridad ${formatScore(score)} de 100`;
}

/**
 * The argument in one line: the score and what each signal contributed.
 *
 * This is the *accessible* half of the pair, so it stays a sentence. A screen
 * reader announces it as the name of the score, and twenty-two of them are read
 * one at a time while the operator arrows down the grid — the engine's full
 * reasoning belongs in {@link scoreArgument}, which nobody has to listen to.
 */
export function scoreSummary(
  score: number,
  reasons: readonly ScoreReason[],
): string {
  if (reasons.length === 0) return `${scoreHead(score)} · ${NO_BREAKDOWN}`;
  const signals = reasons.map((line) => `${line.label} ${line.points}`);
  return [scoreHead(score), ...signals].join(" · ");
}

/**
 * The argument in full: one line per signal, with the sentence the engine wrote.
 *
 * Rendered into `title`, where a pointer reaches it without leaving the grid —
 * the score and its breakdown one gesture apart, never a page away
 * (DESIGN.md §Do's). The reasons are the engine's own prose and are reproduced
 * verbatim; the plate on the project's detail is the same document drawn out.
 */
export function scoreArgument(
  score: number,
  reasons: readonly ScoreReason[],
): string {
  if (reasons.length === 0) return `${scoreHead(score)} · ${NO_BREAKDOWN}.`;
  const lines = reasons.map(
    (line) => `${line.label} ${line.points} — ${line.reason}`,
  );
  return [scoreHead(score), ...lines].join("\n");
}

/**
 * The aggregate attributes a transition's `requires_fields` can name, and
 * whether each is currently empty on this project.
 *
 * A transition demanding a field the project has not filled in renders dead
 * with the field named, which is the whole reason the API ships
 * `requires_fields` even though it changes nothing about the request body.
 *
 * An attribute this build does not know counts as **empty**: the honest answer
 * to a contract that grew is a button that refuses and says which field it is
 * waiting for, not a button that posts and is refused by the server.
 */
export function emptyAggregateFields(
  project: ProjectDetail,
): readonly string[] {
  const values: Readonly<Record<string, unknown>> = {
    name: project.name,
    summary: project.summary ?? null,
    client: project.client,
    owner: project.owner ?? null,
    engagement_type: project.engagement_type,
    project_type: project.project_type ?? null,
    stage: project.stage ?? null,
    start_date: project.start_date ?? null,
    target_date: project.target_date ?? null,
    business_value: project.business_value ?? null,
    next_step: project.next_step ?? null,
  };

  const empty = new Set<string>();
  for (const transition of project.transitions) {
    for (const field of transition.requires_fields) {
      const value = values[field];
      if (value === undefined || value === null || value === "")
        empty.add(field);
    }
  }
  return [...empty];
}

/**
 * The one line describing a forced rank: what was forced, by whom, when.
 *
 * Shared by the server render and by the island that patches the plate after an
 * override is saved — written twice, the two would eventually disagree about
 * the very decision this line exists to record.
 */
export function overrideSummary(forced: Override | null): string {
  if (forced === null) return "";
  const amount =
    forced.position !== null && forced.position !== undefined
      ? `Posición ${forced.position}`
      : `Impulso ${forced.boost ?? ""}`;
  return `${amount} · ${forced.actor} · ${formatInstant(forced.created_at) ?? ""}`;
}

/**
 * Who owns an open blocker, since when, and how long it has been open.
 *
 * Shared by the server render and by the island that rebuilds the list after a
 * write, so the two cannot phrase the same fact differently.
 */
export function blockerMeta(blocker: Blocker): string {
  const owner = blocker.owner?.label ?? "Sin responsable";
  const raised = relativeTime(blocker.raised_at) ?? "";
  return `${owner} · ${raised} · ${blocker.age_days} d abierto`;
}

/** When a blocker was cleared, and how long it had been standing. */
export function resolvedBlockerMeta(blocker: Blocker): string {
  const resolved = relativeTime(blocker.resolved_at ?? null) ?? "";
  return `Resuelto ${resolved} · ${blocker.age_days} d abierto`;
}

/**
 * Collects the facets present in the fetched rows.
 *
 * Built from the data rather than from a fixed list, so a workflow state or an
 * engagement type added in the admin appears as a pill with no frontend change,
 * and a value nobody uses does not offer a filter that returns nothing.
 */
export function collectFacets(
  projects: readonly ProjectPresentation[],
): Facets {
  const categories = new Map<string, number>();
  const engagements = new Map<string, { label: string; count: number }>();
  let atRisk = 0;

  for (const project of projects) {
    categories.set(
      project.stateCategory,
      (categories.get(project.stateCategory) ?? 0) + 1,
    );
    const engagement = engagements.get(project.engagementCode);
    engagements.set(project.engagementCode, {
      label: project.engagementLabel,
      count: (engagement?.count ?? 0) + 1,
    });
    if (project.healthCode !== "HEALTHY") atRisk += 1;
  }

  return {
    categories: [...categories.entries()]
      .map(([value, count]) => ({
        value,
        label: categoryLabel(value),
        count,
      }))
      .sort((a, b) => b.count - a.count),
    engagements: [...engagements.entries()]
      .map(([value, entry]) => ({
        value,
        label: entry.label,
        count: entry.count,
      }))
      .sort((a, b) => b.count - a.count),
    atRisk,
  };
}
