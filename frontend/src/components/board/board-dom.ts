/**
 * The board's DOM vocabulary: every selector the runtime uses, and the patches it applies.
 *
 * The board is server-rendered and then patched — never re-rendered from an event payload
 * (`docs/standards/PATTERNS_FRONTEND.md` §6). Five callers mutate the same nodes (the two drag
 * controllers, the "Mover a…" menu, the member filter and the SSE handlers), so the selectors
 * and the patches live here once. A selector string that appears in two files is how a renamed
 * data attribute becomes a silently dead island.
 *
 * **One vocabulary, two surfaces.** The rail (projects, stacked vertically) and the board
 * (tasks, laid out horizontally) use the *same* attributes for the same things: a drop zone is
 * `[data-column]` on both, a draggable card is `[data-card]` on both. They are told apart by
 * the container they live in — `[data-rail]` or `[data-task-board]` — which is what lets the
 * drag controller, the counts and the lock chips be written once and applied to either axis.
 *
 * Nothing in this module talks to the network or decides policy; it reads and writes DOM.
 */

/**
 * The `MenuSelect` facet the engagement field is mounted under.
 *
 * Read by three places that must agree: the component that renders the control, the inline
 * pre-paint script that restores the stored choice, and the island that owns every later
 * switch. `lib/ui/menu-select.ts` looks the control up by this string.
 */
export const ENGAGEMENT_FACET = "engagement";

/** Attribute selectors of the board. Change a name here and every caller follows. */
export const SEL = {
  board: "[data-board]",
  /** The vertical rail: the projects half, and its own scroller. */
  rail: "[data-rail]",
  /** The horizontal task board: the tasks half. */
  taskBoard: "[data-task-board]",
  /** One engagement type's half of a surface; there is one inside each container. */
  panel: "[data-panel]",
  /** The element that scrolls while a card is carried over it, on either surface. */
  scroller: "[data-scroller]",
  column: "[data-column]",
  columnBody: "[data-column-body]",
  columnCount: "[data-column-count]",
  columnEmpty: "[data-column-empty]",
  columnLock: "[data-column-lock]",
  columnLockText: "[data-column-lock-text]",
  card: "[data-card]",
  cardState: "[data-card-state]",
  cardTitle: "[data-card-title]",
  cardClient: "[data-card-client]",
  cardOwner: "[data-card-owner]",
  cardScore: "[data-card-score]",
  cardScoreSlot: "[data-card-score-slot]",
  cardScoreNote: "[data-card-score-note]",
  cardPending: "[data-card-pending]",
  moveTrigger: "[data-move-trigger]",
  moveMenu: "[data-move-menu]",
  moveMenuList: "[data-move-menu-list]",
  moveMenuTitle: "[data-move-menu-title]",
  staleBanner: "[data-board-stale]",
  staleText: "[data-board-stale-text]",
  staleAction: "[data-board-stale-action]",
  /** The engagement field: a `MenuSelect`, never a `<select>` (`FRONTEND.md` §10). */
  engagementControl: `[data-menu-select][data-facet="${ENGAGEMENT_FACET}"]`,
  member: "[data-member]",
  membersMore: "[data-members-more]",
} as const;

/** How long the attribution chip of a remote move stays legible before it fades. */
export const ATTRIBUTION_LINGER_MS = 1_800;

/**
 * Where the chosen engagement type is remembered.
 *
 * Read twice on purpose: by the inline pre-paint script (so the board never flashes the
 * wrong wall) and by the island that owns every later switch. Both import this constant, so
 * the two readers can never drift onto different keys.
 */
export const BOARD_ENGAGEMENT_KEY = "aztec.ui.board.engagement";

/** The query parameter that carries the selected project, so a reload restores the board. */
export const BOARD_PROJECT_PARAM = "project";

/** The card of one project or task inside `scope`, or `null` when it is not there. */
export function findCard(scope: ParentNode, code: string): HTMLElement | null {
  return scope.querySelector<HTMLElement>(
    `${SEL.card}[data-code="${cssEscape(code)}"]`,
  );
}

/** The drop zone one element sits in, or `null` when it is not inside one. */
export function columnOf(element: Element): HTMLElement | null {
  return element.closest<HTMLElement>(SEL.column);
}

/** The engagement panel one element sits in, or `null` when it is not inside one. */
export function panelOf(element: Element): HTMLElement | null {
  return element.closest<HTMLElement>(SEL.panel);
}

/** Whichever half of the surface an element belongs to: the rail, or the task board. */
export function surfaceOf(element: Element): HTMLElement | null {
  return element.closest<HTMLElement>(`${SEL.rail}, ${SEL.taskBoard}`);
}

/** The visible panel of one container (the rail or the task board). */
export function activePanel(container: ParentNode): HTMLElement | null {
  return container.querySelector<HTMLElement>(`${SEL.panel}:not([hidden])`);
}

/** The panel of one engagement type inside a container, or `null` when it has none. */
export function findPanel(
  container: ParentNode,
  engagementCode: string,
): HTMLElement | null {
  return container.querySelector<HTMLElement>(
    `${SEL.panel}[data-engagement-code="${cssEscape(engagementCode)}"]`,
  );
}

/** The zone of one workflow state inside a panel, or `null` when that state has none. */
export function findColumn(
  panel: ParentNode,
  stateCode: string,
): HTMLElement | null {
  return panel.querySelector<HTMLElement>(
    `${SEL.column}[data-state-code="${cssEscape(stateCode)}"]`,
  );
}

/** Every zone of one panel, in DOM order — the operator's own workflow order. */
export function columnsOf(panel: ParentNode): HTMLElement[] {
  return [...panel.querySelectorAll<HTMLElement>(SEL.column)];
}

/** The card list of one zone — the element cards are appended to. */
export function bodyOf(column: ParentNode): HTMLElement | null {
  return column.querySelector<HTMLElement>(SEL.columnBody);
}

/** The cards currently in one zone, in DOM order, filtered out or not. */
export function cardsOf(column: ParentNode): HTMLElement[] {
  return [...column.querySelectorAll<HTMLElement>(SEL.card)];
}

/**
 * The cards a person can actually see in one zone.
 *
 * The member filter hides cards rather than removing them — a filtered board must snap back
 * without a refetch, and a removed card loses its pending transition — so every count, every
 * keyboard hop and every empty placeholder reads this list and not {@link cardsOf}.
 */
export function visibleCardsOf(column: ParentNode): HTMLElement[] {
  return cardsOf(column).filter((card) => !card.hidden);
}

/** The state label a zone renders, for the copy of toasts and lock chips. */
export function labelOf(column: HTMLElement): string {
  return column.dataset["stateLabel"] ?? column.dataset["stateCode"] ?? "";
}

/**
 * Rewrites a zone's count chip and toggles its empty line.
 *
 * Called on every DOM move and on every filter change, not on confirmation: the count
 * describes what is on screen, and a card sitting in a zone while its transition is still
 * pending is on screen. A zone emptied by the filter says "Sin proyectos" for the same
 * reason a genuinely empty one does — the placeholder answers "is anything here?", which is
 * the only question the reader asked.
 */
export function refreshColumn(column: HTMLElement): void {
  const count = visibleCardsOf(column).length;
  const chip = column.querySelector<HTMLElement>(SEL.columnCount);
  if (chip !== null) chip.textContent = String(count);
  const empty = column.querySelector<HTMLElement>(SEL.columnEmpty);
  if (empty !== null) empty.hidden = count > 0;
}

/**
 * Marks a card as awaiting the server's word: reduced opacity plus a spinner chip.
 *
 * The card is *not* repainted into its new state — the paint is not optimistic
 * (`docs/standards/PATTERNS_FRONTEND.md` §8). While pending, the card sits in the target
 * zone while its own state chip still names where the server believes it is; the two
 * disagreeing is the honest picture of an unconfirmed move.
 */
export function setPending(card: HTMLElement, pending: boolean): void {
  card.dataset["pending"] = pending ? "true" : "false";
  card.setAttribute("aria-busy", pending ? "true" : "false");
  const chip = card.querySelector<HTMLElement>(SEL.cardPending);
  if (chip !== null) chip.hidden = !pending;
}

/** Whether a card is currently waiting for its own transition to be confirmed. */
export function isPending(card: HTMLElement): boolean {
  return card.dataset["pending"] === "true";
}

/**
 * Snaps a card's state chip onto the state of the zone it now lives in.
 *
 * The label and the tone are read from the zone's own dataset rather than from the event
 * payload, because the payload carries a state `code` and no label or colour — the zone is
 * the only place the rendered taxonomy is available (Data-Owns-Color).
 */
export function patchStateChip(card: HTMLElement, column: HTMLElement): void {
  const chip = card.querySelector<HTMLElement>(SEL.cardState);
  card.dataset["stateCode"] = column.dataset["stateCode"] ?? "";
  if (chip === null) return;
  applyStateChip(chip, column);
}

/**
 * Paints one chip with a zone's rendered state: its label, its tone class and the inline
 * `--tone-solid` the operator's own colour arrives as.
 *
 * Shared by the card's chip and the task board's header chip, which must never disagree about
 * where the server believes a project is.
 */
export function applyStateChip(chip: HTMLElement, column: HTMLElement): void {
  const text = chip.querySelector<HTMLElement>("[data-card-state-text]");
  if (text !== null) text.textContent = labelOf(column);
  else chip.textContent = labelOf(column);
  // Tone classes are swapped one by one rather than by rewriting `className`: the element
  // also carries Astro's scope class, and dropping it strips the component's own styles.
  for (const existing of [...chip.classList]) {
    if (existing.startsWith("tone-")) chip.classList.remove(existing);
  }
  const toneClass = column.dataset["toneClass"] ?? "";
  if (toneClass !== "") chip.classList.add(toneClass);
  chip.setAttribute("style", column.dataset["toneStyle"] ?? "");
}

/**
 * Whether an envelope is newer than what the card currently shows.
 *
 * A patch older than the rendered `updated_at` is dropped, never applied: the server render
 * is authoritative until the stream proves it stale (`docs/standards/FRONTEND.md` §6).
 * A card with no watermark — a task card, whose `TaskView` carries no `updated_at` — accepts
 * every patch, because there is nothing to compare against and refusing would freeze it.
 */
export function isNewer(card: HTMLElement, occurredAt: string): boolean {
  const current = card.dataset["updatedAt"] ?? "";
  if (current === "") return true;
  const rendered = Date.parse(current);
  const arriving = Date.parse(occurredAt);
  if (Number.isNaN(rendered) || Number.isNaN(arriving)) return true;
  return arriving > rendered;
}

/** Advances the card's stale-patch watermark; never moves it backwards. */
export function markUpdatedAt(card: HTMLElement, occurredAt: string): void {
  if (isNewer(card, occurredAt)) card.dataset["updatedAt"] = occurredAt;
}

/**
 * Attaches the attribution chip that rides on a card during a remote move and fades once it
 * settles ("Valentina → En progreso", DESIGN.md §Motion).
 *
 * The chip is appended to the card itself so it travels with it for free, and it is removed
 * rather than hidden: a chip left behind would claim authorship of the next move too.
 */
export function rideAttribution(card: HTMLElement, text: string): void {
  const previous = card.querySelector<HTMLElement>("[data-attribution]");
  if (previous !== null) previous.remove();
  const chip = document.createElement("span");
  chip.className = "chip pill card-attribution tone-cielo";
  chip.dataset["attribution"] = "true";
  chip.lang = "es";
  chip.textContent = text;
  card.appendChild(chip);
  window.setTimeout(() => {
    chip.dataset["leaving"] = "true";
    window.setTimeout(() => chip.remove(), 400);
  }, ATTRIBUTION_LINGER_MS);
}

/**
 * Escapes a value for use inside an attribute selector.
 *
 * Project, task and state codes are business identifiers (`PRJ-01`, `PRJ-01-T02`,
 * `en_ejecucion`), but they are server data and a code carrying a quote would otherwise build
 * a selector that throws and takes the whole handler down with it.
 */
function cssEscape(value: string): string {
  return value.replace(/["\\]/g, "\\$&");
}
