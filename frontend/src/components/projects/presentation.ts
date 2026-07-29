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

import type { QueueItem } from "../../lib/api/domain";
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
