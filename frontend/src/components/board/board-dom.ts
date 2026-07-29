/**
 * The board's DOM vocabulary: every selector the runtime uses, and the patches it applies.
 *
 * The board is server-rendered and then patched — never re-rendered from an event payload
 * (`docs/standards/PATTERNS_FRONTEND.md` §6). Three callers mutate the same nodes (the drag
 * controller, the "Mover a…" menu and the SSE handlers), so the selectors and the patches
 * live here once. A selector string that appears in two files is how a renamed data attribute
 * becomes a silently dead island.
 *
 * Nothing in this module talks to the network or decides policy; it reads and writes DOM.
 */

/** Attribute selectors of the board. Change a name here and every caller follows. */
export const SEL = {
  board: "[data-board]",
  panel: "[data-panel]",
  column: "[data-column]",
  columnBody: "[data-column-body]",
  columnCount: "[data-column-count]",
  columnEmpty: "[data-column-empty]",
  columnLock: "[data-column-lock]",
  columnLockText: "[data-column-lock-text]",
  card: "[data-card]",
  cardState: "[data-card-state]",
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
  switcher: "[data-engagement-switcher]",
  switcherPill: "[data-engagement-pill]",
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

/** The card of one project inside `root`, or `null` when it is not on this board. */
export function findCard(root: ParentNode, code: string): HTMLElement | null {
  return root.querySelector<HTMLElement>(`${SEL.card}[data-code="${cssEscape(code)}"]`);
}

/** The column one element sits in, or `null` when it is not inside one. */
export function columnOf(element: Element): HTMLElement | null {
  return element.closest<HTMLElement>(SEL.column);
}

/** The engagement panel one element sits in, or `null` when it is not inside one. */
export function panelOf(element: Element): HTMLElement | null {
  return element.closest<HTMLElement>(SEL.panel);
}

/** The column of one workflow state inside a panel, or `null` when that state has none. */
export function findColumn(panel: ParentNode, stateCode: string): HTMLElement | null {
  return panel.querySelector<HTMLElement>(
    `${SEL.column}[data-state-code="${cssEscape(stateCode)}"]`,
  );
}

/** Every column of one panel, in DOM order (category order, per the model). */
export function columnsOf(panel: ParentNode): HTMLElement[] {
  return [...panel.querySelectorAll<HTMLElement>(SEL.column)];
}

/** The card list of one column — the element cards are appended to. */
export function bodyOf(column: ParentNode): HTMLElement | null {
  return column.querySelector<HTMLElement>(SEL.columnBody);
}

/** The cards currently in one column, in DOM order. */
export function cardsOf(column: ParentNode): HTMLElement[] {
  return [...column.querySelectorAll<HTMLElement>(SEL.card)];
}

/** The state label a column renders, for the copy of toasts and lock chips. */
export function labelOf(column: HTMLElement): string {
  return column.dataset["stateLabel"] ?? column.dataset["stateCode"] ?? "";
}

/**
 * Rewrites a column's count chip and toggles its empty line.
 *
 * Called on every DOM move, not on confirmation: the count describes what is on screen, and
 * a card sitting in a column while its transition is still pending is on screen.
 */
export function refreshColumn(column: HTMLElement): void {
  const count = cardsOf(column).length;
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
 * column while its own state chip still names where the server believes it is; the two
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
 * Snaps a card's state chip onto the state of the column it now lives in.
 *
 * The label and the tone are read from the column's own dataset rather than from the event
 * payload, because the payload carries a state `code` and no label or colour — the column is
 * the only place the rendered taxonomy is available (Data-Owns-Color).
 */
export function patchStateChip(card: HTMLElement, column: HTMLElement): void {
  const chip = card.querySelector<HTMLElement>(SEL.cardState);
  card.dataset["stateCode"] = column.dataset["stateCode"] ?? "";
  if (chip === null) return;
  const text = chip.querySelector<HTMLElement>("[data-card-state-text]");
  if (text !== null) text.textContent = labelOf(column);
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
 * Project and state codes are business identifiers (`PRJ-01`, `en_ejecucion`), but they are
 * server data and a code carrying a quote would otherwise build a selector that throws and
 * takes the whole handler down with it.
 */
function cssEscape(value: string): string {
  return value.replace(/["\\]/g, "\\$&");
}
