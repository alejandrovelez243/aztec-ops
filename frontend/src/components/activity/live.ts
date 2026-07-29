/**
 * The feed's behaviour: live filtering, and keeping the page true while the
 * operator reads it.
 *
 * **Filtering is live, and the URL is still the state.** Every control applies
 * on change — a chosen option, a toggled chip, a date — and the query string is
 * rewritten in the same beat with `replaceState`, so the feed stays a link
 * somebody can paste into a thread while the back button does not collect one
 * entry per keystroke. Free text is debounced because `PRJ-22` is one question,
 * not six; discrete controls fire immediately, because a click already *is* the
 * decision.
 *
 * **Responses cannot race.** Every read takes a sequence number and only the
 * newest one is allowed to paint: a slow request issued three keystrokes ago
 * must never overwrite the answer to the question the operator is actually
 * asking. A refetch marks the region pending instead of collapsing it to a
 * skeleton — the records on screen are still the best answer available until a
 * better one arrives.
 *
 * **An envelope is never rendered as a feed row.** The stream announces domain
 * events; the feed shows `ActivityRecord`s, which are written in the same
 * transaction but are a different thing with a different vocabulary — the verbs,
 * the `from`/`to` values, the `origin` and the `correlation_id` that ties a
 * reprioritization together. So an envelope only means "re-read page one under
 * the facets on screen", and the records themselves come from the API. That is
 * also why this module re-reads rather than patches: a correlated pair arriving
 * as two envelopes must land as one grouped decision, and grouping is a property
 * of the page, not of a row.
 *
 * **It never re-reads what the server already rendered.** The first read happens
 * in the page's frontmatter; this module reads again only when the operator
 * changes a facet, when the stream says something moved, or after a
 * `stream.reset` frame tells it local state can no longer be trusted
 * (`docs/standards/PATTERNS_FRONTEND.md` §6).
 *
 * **It only owns page one.** Changing the page is a real navigation — the whole
 * reading changes and a server render is the honest way to get it — so beyond
 * page one a live re-read would shift rows under the reader's eyes and is not
 * performed.
 */

import {
  ACTIVITY_PAGE_SIZE,
  hrefForCorrelation,
  hrefForPage,
  NO_FILTERS,
  activeFacetCount,
  pageCount as pagesFor,
  pagerModel,
  parseFilters,
  toQuery,
  toSearchParams,
  type ActivityFilters,
} from "../../lib/activity/filters";
import {
  buildDirectory,
  EMPTY_DIRECTORY,
  groupByCorrelation,
  toRows,
  type ActivityRow,
  type DecisionGroup,
  type Directory,
  type MemberRef,
  type ProjectRef,
} from "../../lib/activity/model";
import {
  SYSTEM_ACTOR,
  SYSTEM_ACTOR_LABEL,
} from "../../lib/activity/vocabulary";
import { getPortfolioActivity } from "../../lib/api/client";
import { errorCopy } from "../../lib/api/error-copy";
import { avatarHue, initials } from "../../lib/auth/session";
import { cloneTemplate, setField } from "../../lib/dom/patch";
import { clearDateRange, mountDateRange } from "../ui/date-range";
import { relativeTime } from "../../lib/format/time";
import {
  onReset,
  onStatus,
  retry,
  subscribe,
  type Status,
} from "../../lib/stream/store";
import type { Topic } from "../../lib/stream/topics";
import { readRoster, type RosterState } from "../../lib/team/roster";
import { mountMenus } from "../../lib/ui/menu";
import { toast } from "../../lib/toast";
import { assertNever } from "../../lib/view-state";
import { FEED_COPY, FILTER_COPY, filterSummaryText, recordsText } from "./copy";

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

/** Typing `PRJ-22` is one question; this is how long the bar waits to hear it. */
const TEXT_DEBOUNCE_MS = 350;

/** Envelopes arrive in bursts (one decision, several records); coalesce them. */
const STREAM_DEBOUNCE_MS = 400;

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
  row: "[data-activity-row]",
  /** The table body the rows live in; rebuilt whole on every read. */
  rows: "[data-activity-rows]",
  empty: "[data-activity-empty]",
  error: "[data-activity-error]",
  body: "[data-activity-body]",
  stale: "[data-activity-stale]",
  staleTime: "[data-activity-stale-time]",
  reconnect: "[data-activity-reconnect]",
  projects: "[data-activity-projects]",
  filters: "[data-activity-filters]",
  summary: "[data-field='filter-summary']",
  clear: "[data-action='clear-filters']",
  menuControl: "[data-menu-select], [data-actor-select]",
  actorSelect: "[data-actor-select]",
  dateRange: "[data-date-range]",
  pager: "[data-pager]",
} as const;

/**
 * Mounts the feed: the filter bar, the live region, and the connection banner.
 *
 * @param root - The element carrying `data-activity-live`.
 * @returns The teardown; drops every subscription, listener and pending timer.
 *   Skipping it leaks handlers that keep re-reading the feed for a page the
 *   operator has already navigated away from.
 */
export function mountActivityLive(root: HTMLElement): () => void {
  const directory = readDirectory(root);
  const seen = new Set<number>(readRenderedIds(root));

  let filters = parseFilters(new URLSearchParams(window.location.search));
  let status: Status = "connecting";
  let lastEventAt: string | null = null;
  let textTimer: ReturnType<typeof setTimeout> | null = null;
  let streamTimer: ReturnType<typeof setTimeout> | null = null;
  /** Only the newest read may paint; older answers are discarded on arrival. */
  let sequence = 0;

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

  /** Reads the feed and paints it, unless a newer read has been issued since. */
  const load = async (): Promise<void> => {
    const ticket = ++sequence;
    setPending(root, true);
    const result = await getPortfolioActivity(
      toQuery(filters, { page: filters.page, pageSize: ACTIVITY_PAGE_SIZE }),
    );
    if (ticket !== sequence) return;
    setPending(root, false);

    if (!result.ok) {
      // The records already on screen stay: a feed that blanks itself on a
      // dropped request is worse than a feed that is one answer behind.
      toast({
        kind: "error",
        title: FEED_COPY.errorTitle,
        detail: errorCopy(result.error).detail,
      });
      return;
    }

    const rows = toRows(result.data.items, directory, new Date());
    renderList(root, groupByCorrelation(rows), seen);
    setField(root, "feed-count", recordsText(result.data.count));
    paintArms(root, filters, rows.length > 0);
    renderPager(root, filters, pagesFor(result.data.count, ACTIVITY_PAGE_SIZE));
  };

  /**
   * Adopts a new reading: the URL, the bar's chrome and the feed, in that order.
   *
   * `replaceState` rather than `pushState` — a filter bar that fills the back
   * stack with one entry per keystroke makes "go back" unusable — and the page
   * always resets to one, because page 7 of a different question is not a page
   * the reader asked for.
   */
  const apply = (next: ActivityFilters): void => {
    filters = { ...next, page: 1 };
    const query = toSearchParams(filters).toString();
    window.history.replaceState(
      null,
      "",
      query === ""
        ? window.location.pathname
        : `${window.location.pathname}?${query}`,
    );
    paintFilterChrome(root, filters);
    void load();
  };

  const scheduleStreamRead = (): void => {
    // Beyond page one a live re-read would move rows under the reader's cursor.
    if (filters.page !== 1) return;
    if (streamTimer !== null) clearTimeout(streamTimer);
    streamTimer = setTimeout(() => {
      streamTimer = null;
      void load();
    }, STREAM_DEBOUNCE_MS);
  };

  const offs: (() => void)[] = WATCHED.map((topic) =>
    subscribe(topic, (envelope) => {
      if (lastEventAt === null || envelope.occurred_at > lastEventAt) {
        lastEventAt = envelope.occurred_at;
      }
      paintStale();
      scheduleStreamRead();
    }),
  );

  offs.push(
    onStatus((next) => {
      status = next;
      paintStale();
    }),
    onReset(() => {
      scheduleStreamRead();
    }),
    // One popup grammar for both kinds of control; `lib/ui/menu.ts` owns open,
    // close, Escape and the arrow keys, exactly as the owner picker does.
    mountMenus(root, SELECTOR.menuControl, {
      fill: async (control, panel) => {
        if (!control.matches(SELECTOR.actorSelect)) return;
        await fillActorPanel(control, panel);
      },
      choose: async (control, item) => {
        apply(chooseFacet(control, item, filters));
      },
    }),
    // The range is one control picking both ends, and it reports calendar days
    // exactly as the URL carries them — `toQuery` is still what turns them into
    // the day's opening and closing instants.
    mountDateRange(root, {
      onChange: (change) => {
        apply({ ...filters, since: change.since, until: change.until });
      },
    }),
  );

  const onClick = (event: MouseEvent): void => {
    const target = event.target;
    if (!(target instanceof Element)) return;

    if (target.closest(SELECTOR.reconnect) !== null) {
      retry();
      return;
    }
    if (target.closest("[data-action='retry-feed']") !== null) {
      void load();
      return;
    }
    if (target.closest(SELECTOR.clear) !== null) {
      resetControls(root);
      apply(NO_FILTERS);
      return;
    }
    const pill = target.closest<HTMLButtonElement>("[data-facet='verb']");
    if (pill === null) return;
    const pressed = pill.getAttribute("aria-pressed") === "true";
    pill.setAttribute("aria-pressed", String(!pressed));
    apply({ ...filters, verbs: pressedVerbs(root) });
  };
  root.addEventListener("click", onClick);

  const onInput = (event: Event): void => {
    const target = event.target;
    if (!(target instanceof HTMLInputElement)) return;
    if (target.dataset["facet"] !== "entity_id") return;
    const value = target.value.trim();
    if (textTimer !== null) clearTimeout(textTimer);
    textTimer = setTimeout(() => {
      textTimer = null;
      apply({ ...filters, entityId: value === "" ? null : value });
    }, TEXT_DEBOUNCE_MS);
  };
  root.addEventListener("input", onInput);

  paintStale();
  refreshRelativeTimes(root);

  return () => {
    if (textTimer !== null) clearTimeout(textTimer);
    if (streamTimer !== null) clearTimeout(streamTimer);
    root.removeEventListener("click", onClick);
    root.removeEventListener("input", onInput);
    for (const off of offs) off();
  };
}

// --- Filter controls ---------------------------------------------------------

/**
 * Applies one menu choice to the filter state and repaints the control.
 *
 * The control names the facet it edits (`data-facet`) and the item carries the
 * value, so one handler serves every dropdown: adding a facet is a component
 * plus a line in {@link withFacet}, never a new listener.
 */
function chooseFacet(
  control: HTMLElement,
  item: HTMLElement,
  filters: ActivityFilters,
): ActivityFilters {
  const facet = control.dataset["facet"] ?? "";
  const value = item.dataset["value"] ?? "";
  const label = item.dataset["label"] ?? item.textContent?.trim() ?? "";

  control.dataset["value"] = value;
  paintMenuTicks(control, value);
  if (control.matches(SELECTOR.actorSelect)) {
    paintActorTrigger(control, value, label);
  } else {
    setField(control, "menu-current", label);
  }
  return withFacet(filters, facet, value === "" ? null : value);
}

/** Maps a facet key onto the field it edits; an unknown key changes nothing. */
function withFacet(
  filters: ActivityFilters,
  facet: string,
  value: string | null,
): ActivityFilters {
  if (facet === "entity_type") return { ...filters, entityType: value };
  if (facet === "origin") return { ...filters, origin: value };
  if (facet === "actor") return { ...filters, actor: value };
  return filters;
}

/** Keeps the ticks and `aria-checked` of a server-rendered panel honest. */
function paintMenuTicks(control: HTMLElement, value: string): void {
  for (const item of control.querySelectorAll<HTMLElement>(
    "[data-menu-item]",
  )) {
    const isCurrent = (item.dataset["value"] ?? "") === value;
    item.setAttribute("aria-checked", String(isCurrent));
    const tick = item.querySelector<HTMLElement>("[data-option-tick]");
    if (tick !== null) tick.hidden = !isCurrent;
  }
}

/** Repaints the person picker's trigger: the disk, the glyph, the name. */
function paintActorTrigger(
  control: HTMLElement,
  value: string,
  label: string,
): void {
  const kind = actorKind(value);
  control.dataset["kind"] = kind;
  setField(control, "actor-label", value === "" ? FILTER_COPY.any : label);

  const avatar = control.querySelector<HTMLElement>("[data-actor-avatar]");
  if (avatar === null) return;
  if (kind !== "person") {
    avatar.textContent = "";
    avatar.style.removeProperty("--avatar-h");
    return;
  }
  avatar.textContent = initials(label);
  avatar.style.setProperty("--avatar-h", String(avatarHue(value)));
}

/** Which of the three identity marks an actor value wears. */
function actorKind(value: string): "any" | "system" | "person" {
  if (value === "") return "any";
  return value === SYSTEM_ACTOR ? "system" : "person";
}

/** The verbs currently pressed, in the bar's own order. */
function pressedVerbs(root: HTMLElement): readonly string[] {
  const verbs: string[] = [];
  for (const pill of root.querySelectorAll<HTMLElement>(
    "[data-facet='verb'][aria-pressed='true']",
  )) {
    const value = pill.dataset["value"];
    if (value !== undefined && value !== "") verbs.push(value);
  }
  return verbs;
}

/** Returns every control to its "no filter" position, before applying it. */
function resetControls(root: HTMLElement): void {
  const bar = root.querySelector<HTMLElement>(SELECTOR.filters);
  if (bar === null) return;

  for (const pill of bar.querySelectorAll<HTMLElement>("[data-facet='verb']")) {
    pill.setAttribute("aria-pressed", "false");
  }
  for (const input of bar.querySelectorAll<HTMLInputElement>(
    "input[data-facet]",
  )) {
    input.value = "";
  }
  for (const control of bar.querySelectorAll<HTMLElement>(
    SELECTOR.menuControl,
  )) {
    control.dataset["value"] = "";
    paintMenuTicks(control, "");
    if (control.matches(SELECTOR.actorSelect)) {
      paintActorTrigger(control, "", "");
      continue;
    }
    setField(control, "menu-current", FILTER_COPY.any);
  }
  // The range keeps its value in data attributes rather than in a field, so
  // "Limpiar filtros" has to say so explicitly or the trigger would keep
  // reading "28 jul – 4 ago" over an unfiltered feed.
  for (const range of bar.querySelectorAll<HTMLElement>(SELECTOR.dateRange)) {
    clearDateRange(range);
  }
}

/** The summary line and the "Limpiar filtros" control, after every change. */
function paintFilterChrome(root: HTMLElement, filters: ActivityFilters): void {
  const count = activeFacetCount(filters);
  const summary = root.querySelector<HTMLElement>(SELECTOR.summary);
  if (summary !== null) {
    summary.textContent = filterSummaryText(count);
    summary.hidden = count === 0;
  }
  const clear = root.querySelector<HTMLElement>(SELECTOR.clear);
  if (clear !== null) clear.hidden = count === 0;
}

// --- The person picker's panel ----------------------------------------------

/** Renders one non-list arm of the panel: loading, empty, or the failure. */
function note(control: HTMLElement, panel: HTMLElement, text: string): void {
  const line = cloneTemplate(control, "actor-note");
  if (line === null) return;
  setField(line, "note-text", text);
  panel.appendChild(line);
}

/** How much of a person's week the picker reports beside their name. */
function loadMeta(open: number): string {
  return open === 1
    ? FILTER_COPY.loadTasksOne
    : `${open} ${FILTER_COPY.loadTasksMany}`;
}

/** Appends one choosable actor: a person, the system, or "everyone". */
function actorOption(
  control: HTMLElement,
  panel: HTMLElement,
  entry: { value: string; label: string; meta: string },
): void {
  const item = cloneTemplate(control, "actor-option");
  if (item === null) return;
  const isCurrent = (control.dataset["value"] ?? "") === entry.value;
  const kind = actorKind(entry.value);

  item.dataset["value"] = entry.value;
  item.dataset["label"] = entry.label;
  item.dataset["kind"] = kind;
  item.setAttribute("aria-checked", String(isCurrent));
  setField(item, "option-label", entry.label);
  setField(item, "option-meta", entry.meta);

  const avatar = item.querySelector<HTMLElement>("[data-actor-avatar]");
  if (avatar !== null && kind === "person") {
    avatar.style.setProperty("--avatar-h", String(avatarHue(entry.value)));
    avatar.textContent = initials(entry.label);
  }
  const tick = item.querySelector<HTMLElement>("[data-option-tick]");
  if (tick !== null) tick.hidden = !isCurrent;

  panel.appendChild(item);
}

/**
 * Builds the actor panel for one open, from whichever arm the roster settled on.
 *
 * The roster read is shared with the project pickers (`lib/team/roster.ts`), so
 * opening this menu after having assigned an owner costs no request.
 */
async function fillActorPanel(
  control: HTMLElement,
  panel: HTMLElement,
): Promise<void> {
  panel.replaceChildren();
  note(control, panel, FILTER_COPY.rosterLoading);

  const state: RosterState = await readRoster();
  panel.replaceChildren();

  switch (state.kind) {
    case "loading":
      note(control, panel, FILTER_COPY.rosterLoading);
      return;
    case "error": {
      const copy = errorCopy(state.error);
      note(control, panel, `${copy.title}. ${copy.detail}`);
      return;
    }
    case "empty":
      note(control, panel, FILTER_COPY.rosterEmpty);
      return;
    case "ready":
      actorOption(control, panel, {
        value: "",
        label: FILTER_COPY.any,
        meta: FILTER_COPY.anyActorMeta,
      });
      for (const member of state.members) {
        actorOption(control, panel, {
          value: member.alias,
          label: member.label,
          meta: loadMeta(member.open_tasks),
        });
      }
      // Not a person, and never dressed as one: the engine writes records too.
      actorOption(control, panel, {
        value: SYSTEM_ACTOR,
        label: SYSTEM_ACTOR_LABEL,
        meta: FILTER_COPY.systemMeta,
      });
      return;
    default:
      return assertNever(state);
  }
}

// --- Painting the feed -------------------------------------------------------

/** Marks the region as refetching without taking the records off the screen. */
function setPending(root: HTMLElement, pending: boolean): void {
  root.dataset["pending"] = pending ? "true" : "false";
  const body = root.querySelector<HTMLElement>(SELECTOR.body);
  if (body !== null) body.setAttribute("aria-busy", String(pending));
}

/** Shows whichever arm is now true: the records, or the named emptiness. */
function paintArms(
  root: HTMLElement,
  filters: ActivityFilters,
  hasRows: boolean,
): void {
  const error = root.querySelector<HTMLElement>(SELECTOR.error);
  if (error !== null) error.hidden = true;

  const body = root.querySelector<HTMLElement>(SELECTOR.body);
  if (body !== null) body.hidden = !hasRows;

  const empty = root.querySelector<HTMLElement>(SELECTOR.empty);
  if (empty === null) return;
  empty.hidden = hasRows;
  if (hasRows) return;

  // "Todavía no hay actividad" and "no hay actividad con estos filtros" are
  // different facts and need opposite next actions.
  const filtered = activeFacetCount(filters) > 0;
  setField(
    empty,
    "notice-title",
    filtered ? FEED_COPY.emptyFilteredTitle : FEED_COPY.emptyTitle,
  );
  setField(
    empty,
    "notice-detail",
    filtered ? FEED_COPY.emptyFilteredBody : FEED_COPY.emptyBody,
  );
  const action = empty.querySelector<HTMLElement>("[data-notice-action]");
  if (action !== null) action.hidden = !filtered;
}

/**
 * Replaces the table body with the freshly read page.
 *
 * A run of correlated records is introduced by a header row and its members
 * carry `data-in-decision`, which is how the grouping survives a table: rows
 * cannot nest, so the decision is expressed by a spanning row plus a rail rather
 * than by a container.
 */
function renderList(
  root: HTMLElement,
  groups: readonly DecisionGroup[],
  seen: Set<number>,
): void {
  const body = root.querySelector<HTMLElement>(SELECTOR.rows);
  if (body === null) return;
  const fragment = document.createDocumentFragment();

  for (const group of groups) {
    if (group.isDecision) {
      const head = buildDecisionHead(root, group);
      if (head !== null) fragment.append(head);
    }
    for (const row of group.rows) {
      const node = cloneTemplate(root, "activity-row");
      if (node === null) continue;
      fillRow(node, row);
      node.dataset["inDecision"] = String(group.isDecision);
      if (!seen.has(row.id)) {
        seen.add(row.id);
        node.classList.add("is-new");
      }
      fragment.append(node);
    }
  }
  body.replaceChildren(fragment);
}

/** The row that introduces one decision: how many records, and where all of them are. */
function buildDecisionHead(
  root: HTMLElement,
  group: DecisionGroup,
): HTMLElement | null {
  const head = cloneTemplate(root, "activity-decision");
  if (head === null) return null;
  head.dataset["correlation"] = group.correlationId;
  setField(head, "group-count", recordsText(group.rows.length));
  const link = head.querySelector<HTMLAnchorElement>("[data-link='group']");
  if (link !== null) link.href = hrefForCorrelation(group.correlationId);
  return head;
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

  // The subject is rendered from its declared kind: a project links, anything
  // else is named or shown as its bare code, never sent through the project
  // matcher.
  const linked = node.querySelector<HTMLAnchorElement>("[data-link='project']");
  if (linked !== null) {
    linked.hidden = row.subjectHref === null;
    linked.textContent = row.subjectLabel;
    linked.href = row.subjectHref ?? "#";
  }
  const plain = node.querySelector<HTMLElement>("[data-field='subject-plain']");
  if (plain !== null) {
    plain.hidden = row.subjectHref !== null || row.subjectLabel === "";
    plain.textContent = row.subjectLabel;
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

/**
 * Rebuilds the pager after a live filter change.
 *
 * From the same `pagerModel` the server rendered and the same `<template>`s the
 * component ships, so a client-built pager and a server-built one are the same
 * markup. The links stay real links: changing the page is a navigation.
 */
function renderPager(
  root: HTMLElement,
  filters: ActivityFilters,
  pages: number,
): void {
  const pager = root.querySelector<HTMLElement>(SELECTOR.pager);
  if (pager === null) return;
  const model = pagerModel(filters.page, pages);
  pager.hidden = !model.visible;

  setEdge(
    pager,
    "[data-pager-prev]",
    model.hasPrevious ? hrefForPage(filters, filters.page - 1) : null,
  );
  setEdge(
    pager,
    "[data-pager-next]",
    model.hasNext ? hrefForPage(filters, filters.page + 1) : null,
  );

  const numbers = pager.querySelector<HTMLElement>("[data-pager-numbers]");
  if (numbers === null) return;
  const fragment = document.createDocumentFragment();
  const append = (node: HTMLElement | null): void => {
    if (node !== null) fragment.append(node);
  };

  if (model.showFirst) append(pageItem(pager, filters, 1));
  if (model.leadGap) append(cloneTemplate(pager, "pager-gap"));
  for (const number of model.numbers) {
    append(pageItem(pager, filters, number));
  }
  if (model.trailGap) append(cloneTemplate(pager, "pager-gap"));
  if (model.showLast) append(pageItem(pager, filters, pages));

  numbers.replaceChildren(fragment);
}

/** One numbered link, or the non-link that marks the page already shown. */
function pageItem(
  pager: HTMLElement,
  filters: ActivityFilters,
  number: number,
): HTMLElement | null {
  const isCurrent = number === filters.page;
  const item = cloneTemplate(pager, isCurrent ? "pager-current" : "pager-link");
  if (item === null) return null;
  const target = item.querySelector<HTMLElement>(".page-link");
  if (target === null) return item;
  target.textContent = String(number);
  if (target instanceof HTMLAnchorElement) {
    target.href = hrefForPage(filters, number);
  }
  return item;
}

/**
 * Points one edge control at a page, or renders it dead.
 *
 * An anchor with no `href` is not focusable and not clickable, which is exactly
 * what "there is no previous page" means; removing the control instead would
 * make the pager change width as the reader moves through it.
 */
function setEdge(
  pager: HTMLElement,
  selector: string,
  href: string | null,
): void {
  const edge = pager.querySelector<HTMLAnchorElement>(selector);
  if (edge === null) return;
  if (href === null) {
    edge.removeAttribute("href");
    edge.setAttribute("aria-disabled", "true");
    return;
  }
  edge.href = href;
  edge.removeAttribute("aria-disabled");
}

// --- Hydrating from what the server rendered --------------------------------

/** Every plain object in a parsed JSON array, with anything else dropped. */
function readRecords(raw: string | null): readonly Record<string, unknown>[] {
  let parsed: unknown;
  try {
    parsed = JSON.parse(raw ?? "[]");
  } catch {
    return [];
  }
  if (!Array.isArray(parsed)) return [];
  const items: readonly unknown[] = parsed;
  const records: Record<string, unknown>[] = [];
  for (const item of items) {
    if (typeof item !== "object" || item === null || Array.isArray(item)) {
      continue;
    }
    records.push({ ...item });
  }
  return records;
}

/**
 * Reads the names the page embedded: projects by code, people by alias.
 *
 * The page already fetched both to render its rows, so the island hydrates from
 * the DOM instead of making the same two requests again. A missing or malformed
 * block yields an empty directory: rows then show their bare business code,
 * which is honest, rather than blocking the whole live region.
 */
function readDirectory(root: HTMLElement): Directory {
  const holder = root.querySelector(SELECTOR.projects);
  if (holder === null) return EMPTY_DIRECTORY;
  const parsed = readRecords(holder.textContent);
  const projects: ProjectRef[] = [];
  const members: MemberRef[] = [];
  for (const record of parsed) {
    const code = record["code"];
    const name = record["name"];
    if (typeof code === "string" && typeof name === "string") {
      projects.push({ code, name });
      continue;
    }
    const alias = record["alias"];
    const label = record["label"];
    if (typeof alias === "string" && typeof label === "string") {
      members.push({ alias, label });
    }
  }
  return buildDirectory(projects, members);
}

/**
 * Rewrites every server-rendered relative time against the current clock.
 *
 * A server-rendered "hace 2 minutos" is as old as the page: on a surface people
 * leave open all morning the timestamps would quietly become lies.
 */
function refreshRelativeTimes(root: HTMLElement): void {
  for (const node of root.querySelectorAll<HTMLElement>("[data-occurred-at]")) {
    const occurredAt = node.dataset["occurredAt"];
    if (occurredAt === undefined) continue;
    const when = node.querySelector<HTMLElement>("[data-link='when']");
    if (when === null) continue;
    when.textContent = relativeTime(occurredAt) ?? when.textContent;
  }
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
