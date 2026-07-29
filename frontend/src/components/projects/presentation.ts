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

import type {
  ActivityEntry,
  Blocker,
  Override,
  ProjectDetail,
  QueueItem,
} from "../../lib/api/domain";
import { formatInstant, relativeTime } from "./format";
import { categoryLabel } from "./tone";

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
    hasOverride: item.override !== null && item.override !== undefined,
    riskCount: item.risk_flags.length,
    openBlockers: item.open_blockers,
    updatedAt: item.updated_at,
    searchKey: `${item.code} ${item.name} ${item.client_alias}`.toLowerCase(),
  };
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
export function emptyAggregateFields(project: ProjectDetail): readonly string[] {
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
      if (value === undefined || value === null || value === "") empty.add(field);
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
 * What one audit record changed, as "antes → después".
 *
 * Both values are stored as strings by the audit trail and either may be empty
 * — a creation has no "before". The empty string is returned when neither side
 * says anything, and the caller hides the line rather than rendering an arrow
 * between two blanks.
 */
export function activityChange(entry: ActivityEntry): string {
  const from = entry.from_value;
  const to = entry.to_value;
  if (from !== "" && to !== "") return `${from} → ${to}`;
  return to !== "" ? to : from;
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
