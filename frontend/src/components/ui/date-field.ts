/**
 * The live half of {@link ../ui/DateField.astro}: one date, picked or typed.
 *
 * The single-date twin of `date-range.ts`, and the same shape of module: the
 * calendar itself knows nothing about tasks or projects, it reports a chosen
 * `YYYY-MM-DD` (or `null`) and the caller's `submit` performs the write. That
 * is what lets one control serve a task's due date and a project's target date
 * without either of them leaking into here.
 *
 * Day cells are deliberately **not** `[data-menu-item]`. `lib/ui/menu.ts` walks
 * those linearly with Up/Down, which is the wrong model for a grid — a month is
 * two-dimensional and Down means "next week", not "tomorrow". So the panel
 * handles its own clicks and keys, and the focus placed at the end of `fill` is
 * the one that survives.
 *
 * Nothing is painted optimistically: the chip is repainted from the value the
 * server returned, through the same `renderDateControl` the stream half uses,
 * so a refused write leaves the chip reading what the database still holds.
 */

import {
  addDays,
  addMonths,
  dayLongLabel,
  formatTypedDay,
  isSameDay,
  monthMatrix,
  monthYearLabel,
  parseISODay,
  parseTypedDay,
  toISODay,
  todayCivil,
  weekdayHeadings,
  type CivilDate,
} from "../../lib/ui/calendar";
import { closeMenu, mountMenus } from "../../lib/ui/menu";
import { cloneTemplate, setField, setPending } from "../projects/dom";
import { dueState } from "../projects/format";

const SELECTOR = {
  control: "[data-date-control]",
  day: "[data-date-day]",
  grid: "[data-date-grid]",
  weeks: "[data-date-weeks]",
  weekdays: "[data-date-weekdays]",
  input: "[data-date-input]",
  clear: "[data-date-clear]",
  nav: "[data-date-nav]",
} as const;

/** Performs the write a chosen date means. `null` clears the date. */
export type DateSubmit = (
  control: HTMLElement,
  value: string | null,
) => Promise<void>;

/** The month a panel is currently showing; absent once it closes. */
const shown = new WeakMap<HTMLElement, CivilDate>();

/**
 * Wires every date control inside `container`.
 *
 * @param container - Delegation root, so controls cloned into a table row after
 *   mount are wired too.
 * @param submit - Performs the write for the aggregate the control names.
 * @returns The teardown.
 */
export function mountDateFields(
  container: HTMLElement,
  submit: DateSubmit,
): () => void {
  const controller = new AbortController();
  const { signal } = controller;

  const releaseMenus = mountMenus(container, SELECTOR.control, {
    fill: async (control, panel) => {
      openPanel(control, panel);
    },
    // Every control inside this panel is handled below; see the module note.
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

      const nav = target.closest<HTMLElement>(SELECTOR.nav);
      if (nav !== null) {
        page(control, nav.dataset["dateNav"] === "next-month" ? 1 : -1);
        return;
      }

      if (target.closest(SELECTOR.clear) !== null) {
        void commit(control, null, submit);
        return;
      }

      const day = target.closest<HTMLElement>(SELECTOR.day);
      if (day === null) return;
      const picked = parseISODay(day.dataset["value"]);
      if (picked === null) return;
      void commit(control, toISODay(picked), submit);
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

      if (target.closest(SELECTOR.input) !== null) {
        if (event.key !== "Enter") return;
        event.preventDefault();
        const typed = (target as HTMLInputElement).value.trim();
        // An empty box means "no date"; anything unparseable is left alone so
        // a half-typed date is never read as a request to clear one.
        if (typed === "") {
          void commit(control, null, submit);
          return;
        }
        const parsed = parseTypedDay(typed);
        if (parsed === null) return;
        void commit(control, toISODay(parsed), submit);
        return;
      }

      const day = target.closest<HTMLElement>(SELECTOR.day);
      if (day === null) return;
      const from = parseISODay(day.dataset["value"]);
      if (from === null) return;

      const moved = neighbour(from, event.key);
      if (moved === null) return;
      event.preventDefault();
      shown.set(control, moved);
      buildGrid(control, moved);
      focusDay(control, moved);
    },
    { signal },
  );

  return () => {
    controller.abort();
    releaseMenus();
  };
}

/**
 * Paints one date control from an authoritative value.
 *
 * Shared by every writer and by the stream's repaint, so a date changed here
 * and one changed by a colleague leave the chip in the same state — including
 * its tone, which `dueState` derives against today rather than storing.
 */
export function renderDateControl(
  control: HTMLElement,
  value: string | null,
): void {
  control.dataset["value"] = value ?? "";
  const due = dueState(value);
  setField(control, "date-label", due.label);

  const trigger = control.querySelector<HTMLButtonElement>(
    "[data-menu-trigger]",
  );
  if (trigger === null) return;

  // The tone classes are the chip's whole colour contract; swapping them by
  // hand keeps `dueState` the only place that decides what a date *means*.
  trigger.classList.remove("tone-rojo", "tone-ambar", "tone-piedra");
  trigger.classList.add(due.tone);
  if ("title" in due) trigger.title = due.title;
  else trigger.removeAttribute("title");
  trigger.setAttribute(
    "aria-label",
    `${control.dataset["label"] ?? "Fecha"}. Ahora: ${due.label}`,
  );
}

/** Writes the chosen value, closes the panel, and lets the caller paint. */
async function commit(
  control: HTMLElement,
  value: string | null,
  submit: DateSubmit,
): Promise<void> {
  // Re-choosing the day already stored is not an edit; a PATCH here would be a
  // no-op the server accepts and the timeline would rather not record.
  if ((control.dataset["value"] ?? "") === (value ?? "")) {
    closeMenu(true);
    return;
  }

  const trigger = control.querySelector<HTMLButtonElement>(
    "[data-menu-trigger]",
  );
  closeMenu(true);
  if (trigger !== null) setPending(trigger, true);
  try {
    await submit(control, value);
  } finally {
    if (trigger !== null) {
      setPending(trigger, false);
      // `setPending` disables, which drops focus to `<body>`; a keyboard
      // operator must not be sent to the top of the page by a save.
      if (document.activeElement === document.body) trigger.focus();
    }
  }
}

/** Builds the panel for the month the current value sits in. */
function openPanel(control: HTMLElement, panel: HTMLElement): void {
  const value = parseISODay(control.dataset["value"]);
  const focus = value ?? todayCivil();
  shown.set(control, focus);

  buildWeekdays(control);
  buildGrid(control, focus);

  const input = panel.querySelector<HTMLInputElement>(SELECTOR.input);
  if (input !== null) input.value = formatTypedDay(value);

  // `lib/ui/menu.ts` focuses the first `[data-menu-item]` after `fill`, and
  // this panel has none — so the focus placed here is the one that survives.
  focusDay(control, focus);
}

function page(control: HTMLElement, delta: number): void {
  const current = shown.get(control) ?? todayCivil();
  const next = addMonths(current, delta);
  shown.set(control, next);
  buildGrid(control, next);
}

function buildWeekdays(control: HTMLElement): void {
  const row = control.querySelector<HTMLElement>(SELECTOR.weekdays);
  if (row === null || row.childElementCount > 0) return;
  for (const heading of weekdayHeadings()) {
    const cell = cloneTemplate(control, "date-weekday");
    if (cell === null) continue;
    cell.textContent = heading.initial;
    cell.setAttribute("aria-label", heading.name);
    row.appendChild(cell);
  }
}

/**
 * Redraws the six weeks around `focus`.
 *
 * Exactly one day is `tabindex="0"` — the roving tabstop a grid owes the
 * keyboard: Tab enters the calendar once and the arrows do the walking, rather
 * than Tab visiting forty-two buttons on the way out of the panel.
 */
function buildGrid(control: HTMLElement, focus: CivilDate): void {
  const weeks = control.querySelector<HTMLElement>(SELECTOR.weeks);
  if (weeks === null) return;

  const selected = parseISODay(control.dataset["value"]);
  const today = todayCivil();
  weeks.replaceChildren();

  for (const row of monthMatrix(focus.year, focus.month)) {
    const line = cloneTemplate(control, "date-week");
    if (line === null) continue;
    for (const cell of row) {
      const node = cloneTemplate(control, "date-day");
      if (node === null) continue;
      const button = node.querySelector<HTMLButtonElement>(SELECTOR.day);
      if (button === null) continue;

      const iso = toISODay(cell.date);
      button.textContent = String(cell.date.day);
      button.dataset["value"] = iso;
      button.dataset["outside"] = cell.inMonth ? "false" : "true";
      button.dataset["today"] = isSameDay(cell.date, today) ? "true" : "false";
      button.setAttribute("aria-label", dayLongLabel(cell.date));
      button.setAttribute(
        "aria-selected",
        selected !== null && isSameDay(cell.date, selected) ? "true" : "false",
      );
      button.tabIndex = isSameDay(cell.date, focus) ? 0 : -1;
      line.appendChild(node);
    }
    weeks.appendChild(line);
  }

  setField(control, "date-month", monthYearLabel(focus.year, focus.month));
}

function focusDay(control: HTMLElement, date: CivilDate): void {
  control
    .querySelector<HTMLElement>(
      `${SELECTOR.day}[data-value="${toISODay(date)}"]`,
    )
    ?.focus();
}

/** Where an arrow key lands, in the two dimensions a month actually has. */
function neighbour(from: CivilDate, key: string): CivilDate | null {
  switch (key) {
    case "ArrowLeft":
      return addDays(from, -1);
    case "ArrowRight":
      return addDays(from, 1);
    case "ArrowUp":
      return addDays(from, -7);
    case "ArrowDown":
      return addDays(from, 7);
    case "PageUp":
      return addMonths(from, -1);
    case "PageDown":
      return addMonths(from, 1);
    case "Home":
      return addDays(from, -((from.day - 1) % 7));
    case "End":
      return addDays(from, 6 - ((from.day - 1) % 7));
    default:
      return null;
  }
}
