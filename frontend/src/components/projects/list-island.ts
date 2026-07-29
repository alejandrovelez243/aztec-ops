/**
 * The live half of `/projects`: the view switch, client-side filtering, and the
 * patches the stream delivers.
 *
 * What is *not* here: the first paint. The rows were server-rendered from
 * `GET /api/v1/queue` in the page's frontmatter, so this module reads the DOM
 * it was given and never refetches the list on mount — a fetch on hydration
 * duplicates the request the page already made and flashes the result
 * (`docs/standards/PATTERNS_FRONTEND.md` §6).
 *
 * Filtering is client-side on purpose: the whole portfolio is one page of rows,
 * and a round trip per keystroke would turn a 60ms answer into a spinner. It is
 * also the one interaction the server cannot refuse, which is exactly the test
 * for whether local state is allowed to lead (§8).
 */

import { getProject } from "../../lib/api/client";
import { countUp, flip } from "../../lib/motion/spring";
import { onReset, subscribe, type Envelope } from "../../lib/stream/store";
import type { Topic } from "../../lib/stream/topics";
import {
  applyTone,
  isFresher,
  pulse,
  readNumber,
  readString,
  setField,
} from "./dom";
import { dueState, formatScore } from "./format";
import { markEvent } from "./stale";
import { semanticTone, stateTone } from "./tone";

/** Where the grid ↔ table choice is remembered, per browser. */
const VIEW_KEY = "aztec.ui.projects-view";

/** The two shapes of the same rows. */
type ViewMode = "grid" | "table";

/** Topics that can change something a project row shows. */
const WATCHED: readonly Topic[] = [
  "project.state_changed",
  "project.priority.recalculated",
  "project.updated",
  "blocker.raised",
  "blocker.resolved",
];

/** What the toolbar is currently asking of the rows. */
interface Filters {
  query: string;
  categories: Set<string>;
  engagements: Set<string>;
  onlyAtRisk: boolean;
}

/**
 * Mounts the surface.
 *
 * @param root - The element carrying `data-projects`; both representations of
 *   every project live inside it.
 * @returns The teardown: unsubscribes every stream handler. Skipping it leaves
 *   a detached page patching rows nobody is looking at.
 */
export function mountProjectsList(root: HTMLElement): () => void {
  const filters: Filters = {
    query: "",
    categories: new Set(),
    engagements: new Set(),
    onlyAtRisk: false,
  };

  applyView(root, storedView());
  const controller = new AbortController();
  const { signal } = controller;

  const searchInput = root.querySelector<HTMLInputElement>("[data-search-input]");
  searchInput?.addEventListener(
    "input",
    () => {
      filters.query = searchInput.value.trim().toLowerCase();
      void applyFilters(root, filters);
    },
    { signal },
  );

  root.addEventListener(
    "click",
    (event) => {
      const target = event.target;
      if (!(target instanceof Element)) return;

      const viewButton = target.closest<HTMLElement>("[data-view-button]");
      if (viewButton !== null) {
        const mode = viewButton.dataset.viewButton === "table" ? "table" : "grid";
        rememberView(mode);
        applyView(root, mode);
        return;
      }

      const pill = target.closest<HTMLElement>("[data-filter]");
      if (pill !== null) {
        togglePill(filters, pill);
        void applyFilters(root, filters);
        return;
      }

      if (target.closest("[data-action='clear-filters']") !== null) {
        clearFilters(root, filters, searchInput);
        void applyFilters(root, filters);
        return;
      }

      // A click anywhere on a table row opens it; the name is still a real
      // link, so the keyboard path never depends on this.
      const row = target.closest<HTMLElement>("tr[data-href]");
      if (row !== null && target.closest("a") === null) {
        const href = row.dataset.href;
        if (href !== undefined) window.location.assign(href);
      }
    },
    { signal },
  );

  const offs = WATCHED.map((topic) =>
    subscribe(topic, (envelope) => {
      handleEnvelope(root, envelope);
    }),
  );
  // A reset means the stream could not replay from our cursor, so local state
  // is not merely behind but unknowable: re-read rather than patch.
  offs.push(
    onReset(() => {
      window.location.reload();
    }),
  );

  return () => {
    controller.abort();
    for (const off of offs) off();
  };
}

/** The remembered view, defaulting to the card grid on a first visit. */
function storedView(): ViewMode {
  try {
    return window.localStorage.getItem(VIEW_KEY) === "table" ? "table" : "grid";
  } catch {
    return "grid";
  }
}

/** Persists the choice; a browser refusing storage simply forgets it. */
function rememberView(mode: ViewMode): void {
  try {
    window.localStorage.setItem(VIEW_KEY, mode);
  } catch {
    /* preference not persisted; the surface still switches */
  }
}

/** Switches the surface and tells the toggle which half it is showing. */
function applyView(root: HTMLElement, mode: ViewMode): void {
  root.dataset.view = mode;
  for (const button of root.querySelectorAll<HTMLElement>("[data-view-button]")) {
    button.setAttribute(
      "aria-pressed",
      String(button.dataset.viewButton === mode),
    );
  }
}

/** Adds or removes one pill's value from the filter set it belongs to. */
function togglePill(filters: Filters, pill: HTMLElement): void {
  const pressed = pill.getAttribute("aria-pressed") === "true";
  const value = pill.dataset.value ?? "";
  pill.setAttribute("aria-pressed", String(!pressed));

  if (pill.dataset.filter === "risk") {
    filters.onlyAtRisk = !pressed;
    return;
  }
  const target =
    pill.dataset.filter === "category" ? filters.categories : filters.engagements;
  if (pressed) target.delete(value);
  else target.add(value);
}

/** Releases every filter, including the search box. */
function clearFilters(
  root: HTMLElement,
  filters: Filters,
  searchInput: HTMLInputElement | null,
): void {
  filters.query = "";
  filters.categories.clear();
  filters.engagements.clear();
  filters.onlyAtRisk = false;
  if (searchInput !== null) searchInput.value = "";
  for (const pill of root.querySelectorAll("[data-filter]")) {
    pill.setAttribute("aria-pressed", "false");
  }
}

/** Whether one project element satisfies the current filters. */
function matches(element: HTMLElement, filters: Filters): boolean {
  const haystack = element.dataset.search ?? "";
  if (filters.query !== "" && !haystack.includes(filters.query)) return false;

  const category = element.dataset.category ?? "";
  if (filters.categories.size > 0 && !filters.categories.has(category)) {
    return false;
  }

  const engagement = element.dataset.engagement ?? "";
  if (filters.engagements.size > 0 && !filters.engagements.has(engagement)) {
    return false;
  }

  if (filters.onlyAtRisk && (element.dataset.health ?? "") === "HEALTHY") {
    return false;
  }
  return true;
}

/**
 * Applies the filters to both representations, with the surviving cards
 * gliding into their new positions — the surface's one authored motion moment
 * (DESIGN.md §Motion: objects have mass, nothing teleports).
 *
 * Only elements visible before *and* after are animated: an element appearing
 * has no previous position to travel from, and FLIP-ing it from `(0, 0)` would
 * fling it in from the page corner.
 */
async function applyFilters(root: HTMLElement, filters: Filters): Promise<void> {
  const elements = [...root.querySelectorAll<HTMLElement>("[data-project]")];
  const surviving = elements.filter(
    (element) => !element.hidden && matches(element, filters),
  );

  await flip(surviving, () => {
    for (const element of elements) element.hidden = !matches(element, filters);
  });

  // Each project renders twice — once as a card, once as a row — so the count
  // is over distinct codes, not over elements.
  const visibleCodes = new Set<string>();
  for (const element of elements) {
    const code = element.dataset.code;
    if (!element.hidden && code !== undefined) visibleCodes.add(code);
  }
  const count = visibleCodes.size;

  setField(root, "visible-count", String(count));

  const active =
    filters.query !== "" ||
    filters.categories.size > 0 ||
    filters.engagements.size > 0 ||
    filters.onlyAtRisk;

  const clear = root.querySelector<HTMLElement>("[data-action='clear-filters']");
  if (clear !== null) clear.hidden = !active;

  const empty = root.querySelector<HTMLElement>("[data-filter-empty]");
  if (empty !== null) empty.hidden = count > 0;

  const grid = root.querySelector<HTMLElement>("[data-projects-grid]");
  const table = root.querySelector<HTMLElement>("[data-projects-table]");
  if (grid !== null) grid.hidden = count === 0;
  if (table !== null) table.hidden = count === 0;
}

/** The project a topic is about: the entity itself, or the row it names. */
function projectCodeOf(envelope: Envelope): string | null {
  if (envelope.entity.type === "project") return envelope.entity.id;
  return readString(envelope.payload, "project_code");
}

/** Routes one envelope to the elements that render the project it names. */
function handleEnvelope(root: HTMLElement, envelope: Envelope): void {
  markEvent(envelope.occurred_at);
  const code = projectCodeOf(envelope);
  if (code === null) return;

  const elements = [
    ...root.querySelectorAll<HTMLElement>(`[data-project][data-code="${CSS.escape(code)}"]`),
  ].filter((element) => isFresher(envelope.occurred_at, element.dataset.updatedAt));
  if (elements.length === 0) return;

  if (envelope.topic === "project.priority.recalculated") {
    const value = readNumber(envelope.payload, "value");
    if (value !== null) {
      patchScore(elements, value, envelope.occurred_at);
      return;
    }
  }
  // Everything else changes labels, colours or derived risk, none of which the
  // payload carries: the envelope says *that* it changed, the read says *what*
  // it now is (`docs/EVENTS.md` §4 — payloads carry codes, never labels).
  void refreshRows(elements, code);
}

/** Counts the score up to its new value and marks the row as remotely moved. */
function patchScore(
  elements: readonly HTMLElement[],
  value: number,
  occurredAt: string,
): void {
  for (const element of elements) {
    const figure = element.querySelector<HTMLElement>("[data-field='score']");
    if (figure !== null) {
      const from = Number.parseFloat(figure.textContent ?? "");
      countUp(figure, Number.isFinite(from) ? from : value, value, formatScore);
    }
    element.dataset.updatedAt = occurredAt;
    pulse(element);
  }
}

/** Codes with a read already in flight, so one burst is one request. */
const refreshing = new Set<string>();

/**
 * Re-reads one project and repaints the cells whose values are labels or
 * colours rather than numbers.
 *
 * A failed read is left alone deliberately: the row keeps showing the last
 * value it was rendered with rather than a blank, and the connection banner is
 * what tells the operator the surface may be behind.
 */
async function refreshRows(
  elements: readonly HTMLElement[],
  code: string,
): Promise<void> {
  if (refreshing.has(code)) return;
  refreshing.add(code);
  try {
    const result = await getProject(code);
    if (!result.ok) return;
    const project = result.data;
    const due = dueState(project.target_date ?? null);
    const risks = project.risk_flags.length;

    for (const element of elements) {
      const chip = element.querySelector<HTMLElement>("[data-state-chip]");
      if (chip !== null) {
        applyTone(chip, stateTone(project.state));
        setField(chip, "state-label", project.state.label);
        chip.dataset.category = project.state.category;
      }

      const dueChip = element.querySelector<HTMLElement>("[data-due-chip]");
      if (dueChip !== null) {
        applyTone(dueChip, semanticTone(due.tone));
        setField(dueChip, "due-label", due.label);
        if ("title" in due) dueChip.setAttribute("title", due.title);
        else dueChip.removeAttribute("title");
      }

      const riskChip = element.querySelector<HTMLElement>("[data-field='risk-count']");
      if (riskChip !== null) {
        const spellOut = riskChip.dataset.riskFormat === "words";
        applyTone(riskChip, semanticTone(risks > 0 ? "tone-rojo" : "tone-verde"));
        riskChip.textContent = spellOut
          ? `${risks} ${risks === 1 ? "riesgo" : "riesgos"}`
          : String(risks);
        riskChip.hidden = spellOut && risks === 0;
      }

      element.dataset.category = project.state.category;
      element.dataset.health = project.health.code;
      element.dataset.updatedAt = project.updated_at;
      pulse(element);
    }
  } finally {
    refreshing.delete(code);
  }
}
