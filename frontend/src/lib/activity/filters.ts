/**
 * The feed's filter state, which lives in the URL and nowhere else.
 *
 * `GET /api/v1/activity` sends no next/prev links on purpose: paging and
 * filtering belong in the query string so a narrowed feed is a link somebody can
 * paste into a thread and reopen next week (`backend/apps/shared/pagination.py`).
 * That makes this module the single translator between three representations of
 * one state — the browser's `URLSearchParams`, the typed API query bag, and the
 * `href` of every control that changes it.
 *
 * Framework-free by design: the page parses `Astro.url.searchParams` on the
 * server and the island re-parses `location.search` in the browser, and both must
 * arrive at the same request or a live prepend would silently ignore the facets
 * the operator is looking at.
 */

import type { PortfolioActivityQuery } from "../api/domain";

/** Rows per page. Chosen so one screen of the feed is one scroll, not five. */
export const ACTIVITY_PAGE_SIZE = 25;

/** How many entries the Resumen widget shows: a glance, not a feed. */
export const ACTIVITY_TEASER_SIZE = 5;

/**
 * One reading of the feed: which facets are applied and which page is shown.
 *
 * Absent facets are `null` rather than `""`, because "not filtering by actor"
 * and "filtering by the empty actor" are different questions and only the first
 * one exists.
 */
export interface ActivityFilters {
  readonly entityType: string | null;
  readonly entityId: string | null;
  /** Repeatable; the API ORs the values. */
  readonly verbs: readonly string[];
  readonly actor: string | null;
  readonly origin: string | null;
  /** Calendar day, `YYYY-MM-DD`, as the date input writes it. */
  readonly since: string | null;
  readonly until: string | null;
  /** Set when the reader asked to see one whole decision. */
  readonly correlationId: string | null;
  /** 1-based, clamped: a hand-edited `?page=0` reads page one, not an error. */
  readonly page: number;
}

/** The unfiltered first page — what `/activity` shows with a bare URL. */
export const NO_FILTERS: ActivityFilters = {
  entityType: null,
  entityId: null,
  verbs: [],
  actor: null,
  origin: null,
  since: null,
  until: null,
  correlationId: null,
  page: 1,
};

/** Reads a single-valued parameter; blank and whitespace count as absent. */
function readOne(params: URLSearchParams, key: string): string | null {
  const raw = params.get(key);
  if (raw === null) return null;
  const trimmed = raw.trim();
  return trimmed === "" ? null : trimmed;
}

/**
 * Parses the filter state out of a query string.
 *
 * Nothing is rejected: an unknown verb or a malformed page number narrows to
 * "no matches" or to page one, never to an error screen. The server answers an
 * unknown facet value with an empty page for exactly the same reason.
 */
export function parseFilters(params: URLSearchParams): ActivityFilters {
  const page = Number.parseInt(params.get("page") ?? "", 10);
  return {
    entityType: readOne(params, "entity_type"),
    entityId: readOne(params, "entity_id"),
    verbs: params
      .getAll("verb")
      .map((verb) => verb.trim())
      .filter((verb) => verb !== ""),
    actor: readOne(params, "actor"),
    origin: readOne(params, "origin"),
    since: readOne(params, "since"),
    until: readOne(params, "until"),
    correlationId: readOne(params, "correlation_id"),
    page: Number.isFinite(page) && page > 0 ? page : 1,
  };
}

/**
 * How many facets are narrowing the feed, paging excluded.
 *
 * This is what tells "todavía no hay actividad" apart from "no hay actividad
 * con estos filtros" — two empty screens that need opposite next actions.
 */
export function activeFacetCount(filters: ActivityFilters): number {
  const single = [
    filters.entityType,
    filters.entityId,
    filters.actor,
    filters.origin,
    filters.since,
    filters.until,
    filters.correlationId,
  ].filter((value) => value !== null).length;
  return single + filters.verbs.length;
}

/**
 * A calendar day as the instant the API should compare against.
 *
 * `since` opens the day and `until` closes it, both anchored to UTC — the clock
 * `occurred_at` is recorded in. A bare `YYYY-MM-DD` as `until` would exclude the
 * day the operator just picked, which reads as "my filter lost today's rows".
 */
function dayBoundary(day: string, edge: "start" | "end"): string {
  return edge === "start" ? `${day}T00:00:00Z` : `${day}T23:59:59Z`;
}

/**
 * Builds the typed query bag for one read.
 *
 * `page` is taken from the argument rather than from `filters` so the island can
 * re-read page one under the operator's current facets without pretending they
 * navigated.
 */
export function toQuery(
  filters: ActivityFilters,
  options: { page: number; pageSize: number },
): PortfolioActivityQuery {
  return {
    page: options.page,
    page_size: options.pageSize,
    entity_type: filters.entityType,
    entity_id: filters.entityId,
    verb: [...filters.verbs],
    actor: filters.actor,
    origin: filters.origin,
    since: filters.since === null ? null : dayBoundary(filters.since, "start"),
    until: filters.until === null ? null : dayBoundary(filters.until, "end"),
    correlation_id: filters.correlationId,
  };
}

/** Appends a facet only when it is set, so the URL never grows empty keys. */
function appendIf(
  params: URLSearchParams,
  key: string,
  value: string | null,
): void {
  if (value !== null) params.append(key, value);
}

/**
 * Serializes filter state back into a query string, page included.
 *
 * The key order is fixed so two links to the same reading are the same string —
 * which is what makes a filtered feed shareable rather than merely reachable.
 */
export function toSearchParams(filters: ActivityFilters): URLSearchParams {
  const params = new URLSearchParams();
  appendIf(params, "entity_type", filters.entityType);
  appendIf(params, "entity_id", filters.entityId);
  for (const verb of filters.verbs) params.append("verb", verb);
  appendIf(params, "actor", filters.actor);
  appendIf(params, "origin", filters.origin);
  appendIf(params, "since", filters.since);
  appendIf(params, "until", filters.until);
  appendIf(params, "correlation_id", filters.correlationId);
  if (filters.page > 1) params.append("page", String(filters.page));
  return params;
}

/** The feed's own route; English, like every path (CLAUDE.md §Language). */
const ACTIVITY_PATH = "/activity";

/** `href` of the same reading at another page. */
export function hrefForPage(filters: ActivityFilters, page: number): string {
  const params = toSearchParams({ ...filters, page });
  const query = params.toString();
  return query === "" ? ACTIVITY_PATH : `${ACTIVITY_PATH}?${query}`;
}

/**
 * `href` that expands one whole decision.
 *
 * Every other facet is dropped: a correlation id already names an exact set of
 * records, and keeping a verb filter beside it would hide half the decision the
 * reader asked to see.
 */
export function hrefForCorrelation(correlationId: string): string {
  const params = new URLSearchParams({ correlation_id: correlationId });
  return `${ACTIVITY_PATH}?${params.toString()}`;
}

/**
 * How many pages `count` rows make.
 *
 * At least one: an empty feed still has a page one, and returning zero would
 * make the pager render "página 1 de 0".
 */
export function pageCount(count: number, pageSize: number): number {
  if (pageSize <= 0) return 1;
  return Math.max(1, Math.ceil(count / pageSize));
}
