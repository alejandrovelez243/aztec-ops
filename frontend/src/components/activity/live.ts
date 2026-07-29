/**
 * The feed's live behaviour: keep the first page true while the operator reads
 * it, and say so out loud when the stream drops.
 *
 * **An envelope is never rendered as a feed row.** The stream announces domain
 * events; the feed shows `ActivityRecord`s, which are written in the same
 * transaction but are a different thing with a different vocabulary — the verbs,
 * the `from`/`to` values, the `origin` and the `correlation_id` that ties a
 * reprioritization together. So an envelope only means "re-read the first page
 * under the facets currently on screen", and the records themselves come from
 * the API. That is also why this module re-reads rather than patches: a
 * correlated pair arriving as two envelopes must land as one grouped decision,
 * and grouping is a property of the page, not of a row.
 *
 * **It never re-reads what the server already rendered.** The first read happens
 * in the page's frontmatter; this module reads again only after the stream says
 * something moved, or after a `stream.reset` frame tells it local state can no
 * longer be trusted (`docs/standards/PATTERNS_FRONTEND.md` §6).
 *
 * **It only owns page one.** On any other page a live prepend would silently
 * shift the rows under the reader's eyes, so beyond page one the module keeps
 * the connection status honest and touches nothing else.
 *
 * Relative times are re-rendered on mount because a server-rendered "hace 2
 * minutos" is as old as the page: on a surface people leave open all morning the
 * timestamps would quietly become lies.
 */

import {
  ACTIVITY_PAGE_SIZE,
  hrefForCorrelation,
  parseFilters,
  toQuery,
  type ActivityFilters,
} from "../../lib/activity/filters";
import {
  buildProjectIndex,
  groupByCorrelation,
  toRows,
  type ActivityRow,
  type DecisionGroup,
  type ProjectIndex,
  type ProjectRef,
} from "../../lib/activity/model";
import { getPortfolioActivity } from "../../lib/api/client";
import { cloneTemplate, setField } from "../../lib/dom/patch";
import { relativeTime } from "../../lib/format/time";
import {
  onReset,
  onStatus,
  retry,
  subscribe,
  type Status,
} from "../../lib/stream/store";
import type { Topic } from "../../lib/stream/topics";
import { FEED_COPY, recordsText } from "./copy";

/**
 * Every topic whose handler writes an `ActivityRecord`.
 *
 * Named as constants rather than string literals so a renamed backend topic
 * breaks the build instead of becoming a subscription that never fires.
 */
const WATCHED: readonly Topic[] = [
  "project.created",
  "project.updated",
  "project.state_changed",
  "project.priority.recalculated",
  "task.created",
  "task.updated",
  "task.state_changed",
  "blocker.raised",
  "blocker.resolved",
  "note.added",
];

/** Envelopes arrive in bursts (one decision, several records); coalesce them. */
const REFRESH_DEBOUNCE_MS = 400;

/** Tone classes a row can be wearing before it is repainted. */
const TONE_CLASSES = [
  "tone-rojo",
  "tone-ambar",
  "tone-verde",
  "tone-cielo",
  "tone-piedra",
  "tone-data",
];

const SELECTOR = {
  list: "[data-activity-list]",
  row: "[data-activity-row]",
  rows: "[data-activity-rows]",
  empty: "[data-activity-empty]",
  body: "[data-activity-body]",
  stale: "[data-activity-stale]",
  staleTime: "[data-activity-stale-time]",
  reconnect: "[data-activity-reconnect]",
  projects: "[data-activity-projects]",
} as const;

/**
 * Mounts the feed's live region.
 *
 * @param root - The element carrying `data-activity-live`.
 * @returns The teardown; drops every subscription, the click listener and the
 *   pending refresh. Skipping it leaks handlers that keep re-reading the feed
 *   for a page the operator has already navigated away from.
 */
export function mountActivityLive(root: HTMLElement): () => void {
  const filters = parseFilters(new URLSearchParams(window.location.search));
  const index = readProjectIndex(root);
  const seen = new Set<number>(readRenderedIds(root));

  let status: Status = "connecting";
  let lastEventAt: string | null = null;
  let timer: ReturnType<typeof setTimeout> | null = null;

  refreshRelativeTimes(root);

  const paintStale = (): void => {
    const banner = root.querySelector<HTMLElement>(SELECTOR.stale);
    if (banner === null) return;
    banner.hidden = status !== "disconnected";
    const stamp = banner.querySelector<HTMLElement>(SELECTOR.staleTime);
    if (stamp === null) return;
    const seenAt = relativeTime(lastEventAt);
    stamp.textContent =
      seenAt === null
        ? FEED_COPY.staleNoEvents
        : `${FEED_COPY.staleLastEvent} ${seenAt}`;
  };

  const schedule = (): void => {
    // Beyond page one a prepend would move rows under the reader's cursor.
    if (filters.page !== 1) return;
    if (timer !== null) clearTimeout(timer);
    timer = setTimeout(() => {
      timer = null;
      void reread(root, filters, index, seen);
    }, REFRESH_DEBOUNCE_MS);
  };

  const offs = WATCHED.map((topic) =>
    subscribe(topic, (envelope) => {
      if (lastEventAt === null || envelope.occurred_at > lastEventAt) {
        lastEventAt = envelope.occurred_at;
      }
      paintStale();
      schedule();
    }),
  );

  offs.push(
    onStatus((next) => {
      status = next;
      paintStale();
    }),
    onReset(() => {
      schedule();
    }),
  );

  const onClick = (event: MouseEvent): void => {
    const target = event.target;
    if (!(target instanceof Element)) return;
    if (target.closest(SELECTOR.reconnect) === null) return;
    retry();
  };
  root.addEventListener("click", onClick);

  paintStale();

  return () => {
    if (timer !== null) clearTimeout(timer);
    root.removeEventListener("click", onClick);
    for (const off of offs) off();
  };
}

/**
 * Reads the code→name index the page embedded.
 *
 * The page already fetched the portfolio to render its rows, so the island
 * hydrates from the DOM instead of making the same request again. A missing or
 * malformed block yields an empty index: rows then name their subject by code,
 * which is honest, rather than blocking the whole live region.
 */
function readProjectIndex(root: HTMLElement): ProjectIndex {
  const holder = root.querySelector(SELECTOR.projects);
  if (holder === null) return buildProjectIndex([]);
  let parsed: unknown;
  try {
    parsed = JSON.parse(holder.textContent ?? "[]");
  } catch {
    return buildProjectIndex([]);
  }
  if (!Array.isArray(parsed)) return buildProjectIndex([]);
  const items: readonly unknown[] = parsed;
  const refs: ProjectRef[] = [];
  for (const item of items) {
    if (typeof item !== "object" || item === null || Array.isArray(item)) {
      continue;
    }
    const record: Record<string, unknown> = { ...item };
    const code = record["code"];
    const name = record["name"];
    if (typeof code !== "string" || typeof name !== "string") continue;
    refs.push({ code, name });
  }
  return buildProjectIndex(refs);
}

/** Record ids currently on screen, so a re-read can tell new rows from old. */
function readRenderedIds(root: HTMLElement): number[] {
  const ids: number[] = [];
  for (const node of root.querySelectorAll<HTMLElement>(SELECTOR.row)) {
    const id = Number(node.dataset["id"]);
    if (Number.isFinite(id)) ids.push(id);
  }
  return ids;
}

/** Rewrites every server-rendered relative time against the current clock. */
function refreshRelativeTimes(root: HTMLElement): void {
  for (const node of root.querySelectorAll<HTMLElement>("[data-occurred-at]")) {
    const occurredAt = node.dataset["occurredAt"];
    if (occurredAt === undefined) continue;
    const when = node.querySelector<HTMLElement>("[data-link='when']");
    if (when === null) continue;
    when.textContent = relativeTime(occurredAt) ?? when.textContent;
  }
}

/**
 * Re-reads page one under the facets on screen and rebuilds the list.
 *
 * A failed read changes nothing: the records already rendered stay, because a
 * feed that blanks itself on a dropped request is worse than a feed that is one
 * event behind. The failure surfaces through the connection banner instead.
 */
async function reread(
  root: HTMLElement,
  filters: ActivityFilters,
  index: ProjectIndex,
  seen: Set<number>,
): Promise<void> {
  const result = await getPortfolioActivity(
    toQuery(filters, { page: 1, pageSize: ACTIVITY_PAGE_SIZE }),
  );
  if (!result.ok) return;

  const list = root.querySelector<HTMLElement>(SELECTOR.list);
  if (list === null) return;

  const rows = toRows(result.data.items, index, new Date());
  const groups = groupByCorrelation(rows);
  render(root, list, groups, seen);

  setField(root, "feed-count", recordsText(result.data.count));

  const empty = root.querySelector<HTMLElement>(SELECTOR.empty);
  const body = root.querySelector<HTMLElement>(SELECTOR.body);
  const hasRows = rows.length > 0;
  if (empty !== null) empty.hidden = hasRows;
  if (body !== null) body.hidden = !hasRows;
}

/** Replaces the list with the freshly read page, marking records nobody saw yet. */
function render(
  root: HTMLElement,
  list: HTMLElement,
  groups: readonly DecisionGroup[],
  seen: Set<number>,
): void {
  const fragment = document.createDocumentFragment();
  for (const group of groups) {
    const element = buildGroup(root, group, seen);
    if (element !== null) fragment.append(element);
  }
  list.replaceChildren(fragment);
}

/** Clones one decision group from the component's template and fills it. */
function buildGroup(
  root: HTMLElement,
  group: DecisionGroup,
  seen: Set<number>,
): HTMLElement | null {
  const element = cloneTemplate(root, "activity-group");
  if (element === null) return null;

  element.dataset["correlation"] = group.correlationId;
  element.dataset["decision"] = group.isDecision ? "true" : "false";

  const head = element.querySelector<HTMLElement>("[data-block='group-head']");
  if (head !== null) head.hidden = !group.isDecision;
  setField(element, "group-count", recordsText(group.rows.length));
  const link = element.querySelector<HTMLAnchorElement>("[data-link='group']");
  if (link !== null) link.href = hrefForCorrelation(group.correlationId);

  const host = element.querySelector<HTMLElement>(SELECTOR.rows);
  if (host === null) return element;
  for (const row of group.rows) {
    const node = cloneTemplate(root, "activity-row");
    if (node === null) continue;
    fillRow(node, row);
    if (!seen.has(row.id)) {
      seen.add(row.id);
      node.classList.add("is-new");
    }
    host.append(node);
  }
  return element;
}

/** Writes one record into a cloned row; every field the server sets, set here. */
function fillRow(node: HTMLElement, row: ActivityRow): void {
  node.dataset["id"] = String(row.id);
  node.dataset["origin"] = row.origin;
  node.dataset["occurredAt"] = row.occurredAt;
  node.classList.remove(...TONE_CLASSES);
  node.classList.add(row.originTone);

  setField(node, "verb", row.verbLabel);
  setField(node, "origin", row.originLabel);
  setField(node, "entity-id", row.entityId);
  setField(node, "entity-type", row.entityTypeLabel);
  setField(node, "actor", row.actor);
  setField(node, "reason", row.reason);

  const project = node.querySelector<HTMLAnchorElement>(
    "[data-link='project']",
  );
  if (project !== null) {
    project.hidden = row.projectCode === null;
    project.textContent = row.projectName ?? "";
    project.href =
      row.projectCode === null
        ? "#"
        : `/projects/${encodeURIComponent(row.projectCode)}`;
  }

  setLine(node, "change", row.change);
  setLine(node, "explanation", row.explanation);

  const reason = node.querySelector<HTMLElement>("[data-block='reason']");
  if (reason !== null) reason.hidden = row.reason === "";

  const when = node.querySelector<HTMLElement>("[data-link='when']");
  if (when !== null) {
    when.textContent = row.occurredLabel;
    when.title = row.occurredTitle;
    when.setAttribute("datetime", row.occurredAt);
  }

  const decision = node.querySelector<HTMLAnchorElement>(
    "[data-link='decision']",
  );
  if (decision !== null) decision.href = hrefForCorrelation(row.correlationId);
}

/** Fills a line that disappears when it has nothing to say. */
function setLine(node: HTMLElement, field: string, value: string): void {
  const line = node.querySelector<HTMLElement>(`[data-field='${field}']`);
  if (line === null) return;
  line.textContent = value;
  line.hidden = value === "";
}
