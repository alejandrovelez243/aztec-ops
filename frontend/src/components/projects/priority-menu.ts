/**
 * Re-prioritising a task from the chip that reports its priority.
 *
 * Sibling of `owner-menu.ts`, and deliberately its twin: one painter shared by
 * every surface, one mount that delegates through `lib/ui/menu.ts`, and no
 * optimistic paint anywhere. The chip goes pending, the write goes out, and the
 * priority on screen changes only when the server's own answer comes back —
 * which is the same discipline the owner keeps, for the same reason: the
 * database decides, and a chip flipped ahead of it and rolled back afterwards is
 * worse than a spinner.
 *
 * Nothing here knows a priority code. The options come from the catalog the
 * server rendered into the panel, the current value is read off the DOM, and the
 * tone comes from the row's own `color`. A build that hardcoded "ALTA" would
 * stop painting the priority somebody renamed from the admin.
 */

import { applyTone, setField, setPending } from "./dom";
import { mountMenus } from "../../lib/ui/menu";
import { taxonomyTone } from "./tone";
import type { TaxonomyRef } from "../../lib/api/domain";

/** What the trigger promises; also its accessible name's prefix. */
const ACTION = "Cambiar prioridad";

/**
 * Submits one re-prioritisation.
 *
 * @param control - The control that was used; carries the aggregate's `code`.
 * @param priorityCode - `catalog.Priority.code` of the chosen priority.
 * @returns Resolves when the write has settled and the surface has been painted
 *   with the server's answer. Failures are reported by the implementation.
 */
export type PrioritySubmit = (
  control: HTMLElement,
  priorityCode: string,
) => Promise<void>;

/**
 * Paints one priority control from an authoritative read.
 *
 * The single painter for this control: the task table's live patch, the task
 * header's re-read and the writer below all end here, so a change made on this
 * screen and one arriving over the stream leave the chip identical — down to the
 * ticked option, which is the part that silently drifts when a second painter
 * gets written.
 *
 * @param control - The element carrying `data-priority-control`.
 * @param priority - The priority the server says the task has now.
 */
export function renderPriorityControl(
  control: HTMLElement,
  priority: TaxonomyRef,
): void {
  control.dataset.current = priority.code;

  const trigger = control.querySelector<HTMLButtonElement>(
    "[data-menu-trigger]",
  );
  if (trigger !== null) {
    applyTone(trigger, taxonomyTone(priority.color));
    setField(trigger, "priority-label", priority.label);
    trigger.setAttribute("aria-label", `${ACTION}. Ahora: ${priority.label}`);
  }

  for (const item of control.querySelectorAll<HTMLElement>(
    "[data-menu-item]",
  )) {
    const isCurrent = (item.dataset["value"] ?? "") === priority.code;
    item.setAttribute("aria-checked", String(isCurrent));
    const tick = item.querySelector<HTMLElement>("[data-option-tick]");
    if (tick !== null) tick.hidden = !isCurrent;
  }

  // A row cloned from the table's template arrives carrying the template's own
  // id, so twenty rows would share one. The description is re-keyed to the task
  // this control now edits; duplicate ids make `aria-describedby` resolve to
  // whichever element happens to be first in the document.
  const reason = control.querySelector<HTMLElement>("[data-priority-reason]");
  if (reason === null || trigger === null) return;
  reason.id = `priority-off-${control.dataset["code"] ?? ""}`;
  trigger.setAttribute("aria-describedby", reason.id);
}

/**
 * Wires every priority control inside `container`.
 *
 * @param container - Delegation root; `[data-tasks]` or `[data-task-header]`.
 *   Delegated rather than per control, so a row the stream appended after the
 *   first paint is operable the moment it lands.
 * @param submit - Performs the write for the aggregate the control names.
 * @returns The teardown.
 */
export function mountPriorityMenus(
  container: HTMLElement,
  submit: PrioritySubmit,
): () => void {
  return mountMenus(container, "[data-priority-control]", {
    // The options are the catalog's and the server already rendered them;
    // `renderPriorityControl` keeps the tick honest after every write.
    fill: () => Promise.resolve(),
    choose: async (control, item) => {
      const value = item.dataset["value"] ?? "";
      if (value === "") return;
      // Re-choosing the current priority is not an edit; a PATCH here would be
      // a no-op the server accepts and the timeline would rather not record.
      if ((control.dataset["current"] ?? "") === value) return;

      const trigger = control.querySelector<HTMLButtonElement>(
        "[data-menu-trigger]",
      );
      if (trigger !== null) setPending(trigger, true);
      try {
        await submit(control, value);
      } finally {
        if (trigger !== null) {
          setPending(trigger, false);
          // `setPending` disables, which drops focus to `<body>`; a keyboard
          // operator must not be sent back to the top of the page by a save.
          if (document.activeElement === document.body) trigger.focus();
        }
      }
    },
  });
}
