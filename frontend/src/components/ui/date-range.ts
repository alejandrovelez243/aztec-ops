/**
 * The behaviour of {@link DateRangeField}: one panel that picks both ends.
 *
 * Three modules, three jobs, and the split is deliberate. `lib/ui/menu.ts` owns
 * the popover (open, close, `Escape`, outside click, focus return) and is shared
 * with every other dropdown. `lib/ui/calendar.ts` owns every day of arithmetic
 * and is pure, so it can be reasoned about without a browser. This file owns
 * only the DOM: it reads state, paints attributes, and reports a change.
 *
 * **The URL is the source of truth, not this module.** State is rebuilt from the
 * control's `data-since` / `data-until` on *every* open, and those are written
 * by whoever owns the query string. Reopening a picker after the operator edited
 * the URL, navigated back, or cleared the filters elsewhere shows what the URL
 * says, because there is no surviving component state to disagree with it.
 *
 * **An inverted range is unrepresentable**, not validated: `pickDay`,
 * `setSince` and `setUntil` in the calendar module have no branch that writes an
 * end before its start. Clicking backwards restarts the range, and the hover
 * preview shows that restart before the click happens.
 */

import { cloneTemplate, setField } from "../../lib/dom/patch";
import {
  addDays,
  addMonths,
  announceRange,
  dayLongLabel,
  daysInMonth,
  endOfWeek,
  EMPTY_RANGE,
  formatRangeLabel,
  formatTypedDay,
  isSameDay,
  isWithin,
  monthMatrix,
  monthShortLabel,
  monthYearLabel,
  parseISODay,
  parseTypedDay,
  pickDay,
  previewRange,
  setSince,
  setUntil,
  startOfWeek,
  todayCivil,
  toISODay,
  weekdayHeadings,
  type CivilDate,
  type RangeDraft,
  type RangeValue,
} from "../../lib/ui/calendar";
import { closeMenu, mountMenus } from "../../lib/ui/menu";

/** What the field reports upward: two calendar days, or an open end. */
export interface DateRangeChange {
  readonly since: string | null;
  readonly until: string | null;
}

/** What the owner of the query string does with a change. */
export interface DateRangeHooks {
  /**
   * Called on every settled change — a picked end, a typed date, a clear.
   *
   * Fired while the panel is still open when only the start has been chosen,
   * because "desde el 1 de julio" is already a filter worth answering. The
   * caller is expected to guard concurrent reads; this module does not know
   * what a request is.
   */
  readonly onChange: (change: DateRangeChange) => void;
}

/** What one open panel is doing, kept only while it is open. */
interface PanelState {
  draft: RangeDraft;
  /** Month on screen; not derived from the selection, the operator pages it. */
  visible: { year: number; month: number };
  /** The day the roving `tabindex` sits on. */
  cursor: CivilDate;
  hovered: CivilDate | null;
}

const SELECTOR = {
  control: "[data-date-range]",
  panel: "[data-menu-panel]",
  weekdays: "[data-range-weekdays]",
  weeks: "[data-range-weeks]",
  grid: "[data-range-grid]",
  jump: "[data-range-jump]",
  jumpToggle: "[data-range-months]",
  monthGrid: "[data-range-month-grid]",
  day: "[data-range-day]",
  clear: "[data-range-clear]",
  announce: "[data-range-announce]",
} as const;

/** Spanish, like every string a person reads (CLAUDE.md §Language). */
const COPY = {
  hintStart: "Elige el inicio del rango.",
  hintEnd: "Elige el final. Una fecha anterior reinicia el rango.",
} as const;

/**
 * Which end the next click or keystroke fills.
 *
 * It is a property of the range, not of how it is being edited: a start chosen
 * on the grid and a start typed into the field both leave the *end* as the open
 * question, and the hint, the highlighted field and the hover preview all have
 * to agree about that or the panel is telling the operator two things at once.
 */
function activeEdge(draft: RangeDraft): "since" | "until" {
  if (draft.kind === "picking") return "until";
  const { since, until } = draft.value;
  return since !== null && until === null ? "until" : "since";
}

const states = new WeakMap<HTMLElement, PanelState>();

/**
 * Wires every date-range control inside `container`.
 *
 * @param container - Delegation root; must outlive the controls inside it.
 * @param hooks - What a settled change means to the surface.
 * @returns The teardown; drops every listener and closes an open panel.
 */
export function mountDateRange(
  container: HTMLElement,
  hooks: DateRangeHooks,
): () => void {
  const controller = new AbortController();
  const { signal } = controller;

  const releaseMenus = mountMenus(container, SELECTOR.control, {
    fill: async (control, panel) => {
      openPanel(control, panel);
    },
    // Day cells are deliberately not `[data-menu-item]`: choosing a start must
    // leave the panel open for the end. Every click inside is handled below.
    choose: async () => {
      /* no menu items in this panel */
    },
  });

  container.addEventListener(
    "click",
    (event) => {
      const target = event.target;
      if (!(target instanceof Element)) return;
      const control = target.closest<HTMLElement>(SELECTOR.control);
      if (control === null) return;
      handleClick(control, target, hooks);
    },
    { signal },
  );

  container.addEventListener(
    "keydown",
    (event) => {
      const target = event.target;
      if (!(target instanceof Element)) return;
      const control = target.closest<HTMLElement>(SELECTOR.control);
      if (control === null) return;
      handleKeydown(control, target, event, hooks);
    },
    { signal },
  );

  // `pointerover` rather than `mouseenter`: one delegated listener for
  // forty-two cells that are rebuilt on every month page.
  container.addEventListener(
    "pointerover",
    (event) => {
      const target = event.target;
      if (!(target instanceof Element)) return;
      const control = target.closest<HTMLElement>(SELECTOR.control);
      if (control === null) return;
      const state = states.get(control);
      if (state === undefined) return;
      const day = target.closest<HTMLElement>(SELECTOR.day);
      const hovered = day === null ? null : parseISODay(day.dataset["iso"]);
      if (hovered === null && state.hovered === null) return;
      state.hovered = hovered;
      paintDays(control, state);
    },
    { signal },
  );

  container.addEventListener(
    "change",
    (event) => {
      const target = event.target;
      if (!(target instanceof HTMLInputElement)) return;
      const control = target.closest<HTMLElement>(SELECTOR.control);
      if (control === null) return;
      applyTyped(control, target, hooks);
    },
    { signal },
  );

  return () => {
    controller.abort();
    releaseMenus();
  };
}

// --- Opening -----------------------------------------------------------------

/**
 * Rebuilds the panel from the control's data attributes.
 *
 * Every open starts from the attributes rather than from whatever the last open
 * left behind, which is what makes the URL authoritative: there is no cached
 * selection that could survive a navigation and contradict it.
 */
function openPanel(control: HTMLElement, panel: HTMLElement): void {
  const value = readValue(control);
  const today = todayCivil();
  const anchorDay = value.since ?? value.until ?? today;
  const state: PanelState = {
    draft: { kind: "idle", value },
    visible: { year: anchorDay.year, month: anchorDay.month },
    cursor: anchorDay,
    hovered: null,
  };
  states.set(control, state);

  setJumpOpen(control, false);
  buildWeekdays(control);
  buildGrid(control, state);
  paintPanel(control, state);
  fillTypedInputs(control, state.draft.value);

  // `lib/ui/menu.ts` focuses the first `[data-menu-item]` after `fill`, and this
  // panel has none — so the focus placed here is the one that survives.
  panel.querySelector<HTMLElement>(`${SELECTOR.day}[tabindex="0"]`)?.focus();
}

/** The persisted range, read from the attributes the surface keeps in sync. */
function readValue(control: HTMLElement): RangeValue {
  return {
    since: parseISODay(control.dataset["since"]),
    until: parseISODay(control.dataset["until"]),
  };
}

// --- Events ------------------------------------------------------------------

function handleClick(
  control: HTMLElement,
  target: Element,
  hooks: DateRangeHooks,
): void {
  const state = states.get(control);
  if (state === undefined) return;

  const day = target.closest<HTMLElement>(SELECTOR.day);
  if (day !== null) {
    const picked = parseISODay(day.dataset["iso"]);
    if (picked !== null) choose(control, state, picked, hooks);
    return;
  }

  if (target.closest(SELECTOR.clear) !== null) {
    state.draft = { kind: "idle", value: EMPTY_RANGE };
    state.hovered = null;
    fillTypedInputs(control, EMPTY_RANGE);
    paintPanel(control, state);
    commit(control, EMPTY_RANGE, hooks);
    return;
  }

  if (target.closest(SELECTOR.jumpToggle) !== null) {
    const jump = control.querySelector<HTMLElement>(SELECTOR.jump);
    // `hidden` is `boolean | "until-found"` in the DOM types; only the plain
    // `true` this component sets counts as closed.
    setJumpOpen(control, jump !== null && jump.hidden === true);
    paintPanel(control, state);
    return;
  }

  const month = target.closest<HTMLElement>("[data-range-month-value]");
  if (month !== null) {
    const chosen = Number(month.dataset["rangeMonthValue"]);
    if (Number.isFinite(chosen)) {
      state.visible = { year: state.visible.year, month: chosen };
      state.cursor = clampToVisible(state.cursor, state.visible);
      setJumpOpen(control, false);
      buildGrid(control, state);
      paintPanel(control, state);
    }
    return;
  }

  const nav = target.closest<HTMLElement>("[data-range-nav]");
  if (nav === null) return;
  applyNav(control, state, nav.dataset["rangeNav"] ?? "");
}

/** Month and year stepping, from the header and from the jump layer. */
function applyNav(
  control: HTMLElement,
  state: PanelState,
  direction: string,
): void {
  const steps: Readonly<Record<string, number>> = {
    "previous-month": -1,
    "next-month": 1,
    "previous-year": -12,
    "next-year": 12,
  };
  const step = steps[direction];
  if (step === undefined) return;

  const moved = addMonths({ ...state.visible, day: 1 }, step);
  state.visible = { year: moved.year, month: moved.month };
  state.cursor = clampToVisible(state.cursor, state.visible);
  buildGrid(control, state);
  paintPanel(control, state);
}

function handleKeydown(
  control: HTMLElement,
  target: Element,
  event: KeyboardEvent,
  hooks: DateRangeHooks,
): void {
  const state = states.get(control);
  if (state === undefined) return;

  // `Enter` in a typed field commits there and then. `change` would get there
  // eventually — on blur — but "I typed it and pressed Enter" must not wait for
  // the operator to click somewhere else to be believed.
  if (target instanceof HTMLInputElement && target.dataset["rangeInput"]) {
    if (event.key !== "Enter") return;
    event.preventDefault();
    applyTyped(control, target, hooks);
    return;
  }

  if (target.closest(SELECTOR.day) === null) return;

  const moved = nextCursor(state.cursor, event.key);
  if (moved === null) return;
  event.preventDefault();

  state.cursor = moved;
  if (
    moved.year !== state.visible.year ||
    moved.month !== state.visible.month
  ) {
    state.visible = { year: moved.year, month: moved.month };
    setJumpOpen(control, false);
    buildGrid(control, state);
  }
  paintPanel(control, state);
  focusCursor(control, state);
}

/**
 * Where a key takes the cursor.
 *
 * `null` means "not ours": `Enter` and `Space` are the day button's own click,
 * `Escape` belongs to `lib/ui/menu.ts`, and `Tab` must keep reaching the typed
 * fields — the path a screen-reader user takes instead of the grid.
 */
function nextCursor(cursor: CivilDate, key: string): CivilDate | null {
  switch (key) {
    case "ArrowLeft":
      return addDays(cursor, -1);
    case "ArrowRight":
      return addDays(cursor, 1);
    case "ArrowUp":
      return addDays(cursor, -7);
    case "ArrowDown":
      return addDays(cursor, 7);
    case "PageUp":
      return addMonths(cursor, -1);
    case "PageDown":
      return addMonths(cursor, 1);
    case "Home":
      return startOfWeek(cursor);
    case "End":
      return endOfWeek(cursor);
    default:
      return null;
  }
}

/** Applies one click or `Enter` on a day, and reports the result. */
function choose(
  control: HTMLElement,
  state: PanelState,
  picked: CivilDate,
  hooks: DateRangeHooks,
): void {
  state.draft = pickDay(state.draft, picked);
  state.cursor = picked;
  state.hovered = null;
  if (
    picked.year !== state.visible.year ||
    picked.month !== state.visible.month
  ) {
    state.visible = { year: picked.year, month: picked.month };
    buildGrid(control, state);
  }
  fillTypedInputs(control, state.draft.value);
  paintPanel(control, state);
  commit(control, state.draft.value, hooks);

  if (state.draft.kind === "picking") {
    focusCursor(control, state);
    return;
  }
  // The range is complete: say so, then hand the panel back.
  announce(control, announceRange(state.draft.value));
  closeMenu(true);
}

/**
 * Applies a typed date.
 *
 * An unparseable value marks the field invalid and changes nothing — a filter
 * that silently ignored what somebody typed would be worse than one that says
 * it did not understand.
 */
function applyTyped(
  control: HTMLElement,
  input: HTMLInputElement,
  hooks: DateRangeHooks,
): void {
  const state = states.get(control);
  if (state === undefined) return;
  const edge = input.dataset["rangeInput"];
  if (edge !== "since" && edge !== "until") return;

  const text = input.value.trim();
  const parsed = text === "" ? null : parseTypedDay(text);
  if (text !== "" && parsed === null) {
    input.setAttribute("aria-invalid", "true");
    return;
  }
  input.removeAttribute("aria-invalid");

  const value =
    edge === "since"
      ? setSince(state.draft.value, parsed)
      : setUntil(state.draft.value, parsed);
  state.draft = { kind: "idle", value };
  state.hovered = null;

  const focus = value.since ?? value.until;
  if (focus !== null) {
    state.cursor = focus;
    state.visible = { year: focus.year, month: focus.month };
    buildGrid(control, state);
  }
  fillTypedInputs(control, value);
  paintPanel(control, state);
  commit(control, value, hooks);
}

/**
 * Empties one control from outside, without reporting a change.
 *
 * This is what a surface-wide "Limpiar filtros" calls: it already knows it is
 * about to apply an empty filter set, and a second change from here would send a
 * duplicate request for the reading it is on its way to ask for.
 */
export function clearDateRange(control: HTMLElement): void {
  control.dataset["since"] = "";
  control.dataset["until"] = "";
  setField(control, "range-label", formatRangeLabel(EMPTY_RANGE));
  fillTypedInputs(control, EMPTY_RANGE);
  states.delete(control);
}

/** Writes the new range onto the control and hands it to the surface. */
function commit(
  control: HTMLElement,
  value: RangeValue,
  hooks: DateRangeHooks,
): void {
  const since = value.since === null ? null : toISODay(value.since);
  const until = value.until === null ? null : toISODay(value.until);
  control.dataset["since"] = since ?? "";
  control.dataset["until"] = until ?? "";
  setField(control, "range-label", formatRangeLabel(value));
  hooks.onChange({ since, until });
}

// --- Painting ----------------------------------------------------------------

/** Builds the seven column headers once per open. */
function buildWeekdays(control: HTMLElement): void {
  const row = control.querySelector<HTMLElement>(SELECTOR.weekdays);
  if (row === null) return;
  const fragment = document.createDocumentFragment();
  for (const heading of weekdayHeadings()) {
    const cell = cloneTemplate(control, "range-weekday");
    if (cell === null) continue;
    cell.textContent = heading.initial;
    cell.setAttribute("aria-label", heading.name);
    fragment.append(cell);
  }
  row.replaceChildren(fragment);
}

/** Rebuilds the six weeks of the visible month. Attributes are painted after. */
function buildGrid(control: HTMLElement, state: PanelState): void {
  const host = control.querySelector<HTMLElement>(SELECTOR.weeks);
  if (host === null) return;
  const fragment = document.createDocumentFragment();

  for (const week of monthMatrix(state.visible.year, state.visible.month)) {
    const row = cloneTemplate(control, "range-week");
    if (row === null) continue;
    for (const cell of week) {
      const node = cloneTemplate(control, "range-day");
      if (node === null) continue;
      const button = node.querySelector<HTMLElement>(SELECTOR.day);
      if (button === null) continue;
      button.dataset["iso"] = toISODay(cell.date);
      button.dataset["outside"] = String(!cell.inMonth);
      button.textContent = String(cell.date.day);
      button.setAttribute("aria-label", dayLongLabel(cell.date));
      row.append(node);
    }
    fragment.append(row);
  }
  host.replaceChildren(fragment);
}

/** The header, the jump layer, the hint, the clear control, and the days. */
function paintPanel(control: HTMLElement, state: PanelState): void {
  setField(
    control,
    "range-month",
    monthYearLabel(state.visible.year, state.visible.month),
  );
  setField(control, "range-year", String(state.visible.year));
  const edge = activeEdge(state.draft);
  setField(
    control,
    "range-hint",
    edge === "until" ? COPY.hintEnd : COPY.hintStart,
  );
  for (const field of control.querySelectorAll<HTMLElement>(
    "[data-range-field]",
  )) {
    field.dataset["active"] = String(field.dataset["rangeField"] === edge);
  }

  const value = state.draft.value;
  const clear = control.querySelector<HTMLElement>(SELECTOR.clear);
  if (clear !== null) {
    clear.hidden = value.since === null && value.until === null;
  }

  buildMonthOptions(control, state);
  paintDays(control, state);
}

/** The twelve month buttons of the jump layer, for the year on screen. */
function buildMonthOptions(control: HTMLElement, state: PanelState): void {
  const host = control.querySelector<HTMLElement>(SELECTOR.monthGrid);
  if (host === null) return;
  const fragment = document.createDocumentFragment();
  for (let month = 1; month <= 12; month += 1) {
    const option = cloneTemplate(control, "range-month-option");
    if (option === null) continue;
    option.dataset["rangeMonthValue"] = String(month);
    option.textContent = monthShortLabel(month);
    option.setAttribute("aria-current", String(month === state.visible.month));
    fragment.append(option);
  }
  host.replaceChildren(fragment);
}

/**
 * Paints selection, preview, today and the roving `tabindex` onto the cells that
 * already exist.
 *
 * Attributes only, never a rebuild: hovering must not replace the node the
 * pointer is over, and moving the cursor must not destroy the element that
 * currently holds focus.
 */
function paintDays(control: HTMLElement, state: PanelState): void {
  const span = previewRange(state.draft, state.hovered);
  const today = todayCivil();

  for (const button of control.querySelectorAll<HTMLElement>(SELECTOR.day)) {
    const date = parseISODay(button.dataset["iso"]);
    if (date === null) continue;

    const isStart = span.since !== null && isSameDay(date, span.since);
    const isEnd = span.until !== null && isSameDay(date, span.until);
    const inRange = isWithin(date, span.since, span.until);

    button.dataset["inRange"] = String(inRange);
    button.dataset["selected"] = String(isStart || isEnd);
    button.dataset["today"] = String(isSameDay(date, today));
    button.dataset["edge"] = edgeOf(isStart, isEnd, span);
    button.setAttribute("aria-selected", String(inRange));
    if (isSameDay(date, today)) {
      button.setAttribute("aria-current", "date");
    } else {
      button.removeAttribute("aria-current");
    }
    button.tabIndex = isSameDay(date, state.cursor) ? 0 : -1;
  }
}

/** Which corners of a day in the band stay rounded. */
function edgeOf(isStart: boolean, isEnd: boolean, span: RangeValue): string {
  const openEnded = span.since === null || span.until === null;
  if (isStart && (isEnd || openEnded)) return "both";
  if (isStart) return "start";
  if (isEnd) return "end";
  return "none";
}

/** Puts the keyboard back on the cursor after the grid was rebuilt. */
function focusCursor(control: HTMLElement, state: PanelState): void {
  const iso = toISODay(state.cursor);
  control
    .querySelector<HTMLElement>(`${SELECTOR.day}[data-iso="${iso}"]`)
    ?.focus();
}

/** Fills the typed fields, which mirror the selection rather than lead it. */
function fillTypedInputs(control: HTMLElement, value: RangeValue): void {
  for (const input of control.querySelectorAll<HTMLInputElement>(
    "[data-range-input]",
  )) {
    const edge = input.dataset["rangeInput"];
    input.value = formatTypedDay(edge === "since" ? value.since : value.until);
    input.removeAttribute("aria-invalid");
  }
}

/** Speaks the completed range once, into the panel's own status region. */
function announce(control: HTMLElement, text: string): void {
  const region = control.querySelector<HTMLElement>(SELECTOR.announce);
  if (region !== null) region.textContent = text;
}

/** Shows or hides the month/year jump, keeping its toggle's state honest. */
function setJumpOpen(control: HTMLElement, open: boolean): void {
  const jump = control.querySelector<HTMLElement>(SELECTOR.jump);
  if (jump !== null) jump.hidden = !open;
  const grid = control.querySelector<HTMLElement>(SELECTOR.grid);
  if (grid !== null) grid.hidden = open;
  const toggle = control.querySelector<HTMLElement>(SELECTOR.jumpToggle);
  if (toggle !== null) toggle.setAttribute("aria-expanded", String(open));
}

/**
 * Keeps the cursor inside the month now on screen.
 *
 * The day is clamped rather than reset to the first: paging from 31 March to
 * April must land on the 30th, not throw the operator back to the top of the
 * month they were reading.
 */
function clampToVisible(
  cursor: CivilDate,
  visible: { year: number; month: number },
): CivilDate {
  if (cursor.year === visible.year && cursor.month === visible.month) {
    return cursor;
  }
  return {
    year: visible.year,
    month: visible.month,
    day: Math.min(cursor.day, daysInMonth(visible.year, visible.month)),
  };
}
