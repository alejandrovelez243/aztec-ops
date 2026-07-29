/**
 * Reassigning work from the screen that shows it.
 *
 * The roster is the API's (`GET /api/v1/team/load`), never a list in this file:
 * a person hired this morning appears in the picker with no frontend change,
 * and the load figures beside each name — open tasks, and whether that person is
 * already overloaded — are the same numbers the `OWNER_OVERLOADED` risk is
 * computed from, so the operator is choosing with the evidence in front of them
 * instead of discovering the consequence as a flag afterwards.
 *
 * "Sin responsable" is offered because the API accepts it: `owner` is one of the
 * clearable fields of `PATCH /api/v1/projects/{code}` — an explicitly `null`
 * owner unassigns, an absent one leaves it alone (`ProjectUpdateIn`). Offering a
 * clear the server would refuse would be inventing a capability.
 *
 * Nothing here is painted optimistically. The trigger goes pending, the write
 * goes out, and the owner on screen changes only when the server's own answer
 * comes back.
 */

import { avatarHue, initials } from "../../lib/auth/session";
import { cloneTemplate, setField, setPending } from "./dom";
import { failureCopy } from "./messages";
import { mountMenus } from "../../lib/ui/menu";
import { readRoster, type RosterState } from "../../lib/team/roster";
import { assertNever } from "../../lib/view-state";
import type { ActorRef } from "../../lib/api/domain";

/** The named absence, in the product's words (Present-Absence Rule). */
const UNASSIGNED = "Sin responsable";

/** What the trigger promises; also its accessible name's prefix. */
const ACTION = "Cambiar responsable";

/**
 * Submits one reassignment.
 *
 * @param control - The control that was used; carries the aggregate's `code`.
 * @param alias - `accounts.User.code`, or `null` to unassign.
 * @returns Resolves when the write has settled and the surface has been painted
 *   with the server's answer. Failures are reported by the implementation.
 */
export type OwnerSubmit = (
  control: HTMLElement,
  alias: string | null,
) => Promise<void>;

/**
 * Paints one owner control from an authoritative read.
 *
 * Shared by the server render's live half and by every writer, so the header and
 * a row cannot end up phrasing the same fact differently.
 *
 * @param control - The element carrying `data-owner-control`.
 * @param owner - The current owner, or `null` when nobody owns it.
 */
export function renderOwnerControl(
  control: HTMLElement,
  owner: ActorRef | null,
): void {
  const label = owner?.label ?? UNASSIGNED;
  control.dataset.current = owner?.alias ?? "";
  control.dataset.owned = owner === null ? "false" : "true";
  setField(control, "owner-label", label);

  const avatar = control.querySelector<HTMLElement>("[data-owner-avatar]");
  if (avatar !== null && owner !== null) {
    avatar.style.setProperty("--avatar-h", String(avatarHue(owner.alias)));
    avatar.textContent = initials(owner.label);
  }

  const trigger = control.querySelector<HTMLButtonElement>(
    "[data-menu-trigger]",
  );
  if (trigger === null) return;
  trigger.setAttribute("aria-label", `${ACTION}. Ahora: ${label}`);

  // A row cloned from the template arrives carrying the template's own id, so
  // twenty rows would share one. The description is re-keyed to the aggregate
  // this control now edits; duplicate ids make `aria-describedby` resolve to
  // whichever element happens to be first in the document.
  const reason = control.querySelector<HTMLElement>("[data-owner-reason]");
  if (reason === null) return;
  reason.id = `owner-off-${control.dataset.scope ?? ""}-${control.dataset.code ?? ""}`;
  trigger.setAttribute("aria-describedby", reason.id);
}

/** Renders one non-list arm of the panel: loading, empty, or the failure. */
function note(control: HTMLElement, panel: HTMLElement, text: string): void {
  const line = cloneTemplate(control, "owner-note");
  if (line === null) return;
  setField(line, "note-text", text);
  panel.appendChild(line);
}

/** How much of a person's week the picker reports beside their name. */
function loadMeta(open: number, overloaded: boolean): string {
  const tasks = open === 1 ? "1 tarea abierta" : `${open} tareas abiertas`;
  return overloaded ? `${tasks} · sobrecargado` : tasks;
}

/** Appends one choosable person, or the clear option when `alias` is empty. */
function option(
  control: HTMLElement,
  panel: HTMLElement,
  entry: { alias: string; label: string; meta: string },
): void {
  const item = cloneTemplate(control, "owner-option");
  if (item === null) return;
  const isCurrent = (control.dataset.current ?? "") === entry.alias;

  item.dataset.alias = entry.alias;
  item.dataset.owned = entry.alias === "" ? "false" : "true";
  item.setAttribute("aria-checked", String(isCurrent));
  setField(item, "option-label", entry.label);
  setField(item, "option-meta", entry.meta);

  const avatar = item.querySelector<HTMLElement>("[data-owner-avatar]");
  if (avatar !== null && entry.alias !== "") {
    avatar.style.setProperty("--avatar-h", String(avatarHue(entry.alias)));
    avatar.textContent = initials(entry.label);
  }
  const tick = item.querySelector<HTMLElement>("[data-option-tick]");
  if (tick !== null) tick.hidden = !isCurrent;

  panel.appendChild(item);
}

/** Builds the panel for one open, from whichever arm the roster read settled on. */
async function fillPanel(
  control: HTMLElement,
  panel: HTMLElement,
): Promise<void> {
  panel.replaceChildren();
  note(control, panel, "Cargando el equipo…");

  const state: RosterState = await readRoster();
  // The operator may have closed and reopened while the read was in flight; the
  // panel is rebuilt from scratch either way, so this is safe to apply.
  panel.replaceChildren();

  switch (state.kind) {
    case "loading":
      note(control, panel, "Cargando el equipo…");
      return;
    case "error": {
      // The roster carries the typed failure, not prose: this surface speaks
      // the projects vocabulary, and the same read feeds the activity filter,
      // which speaks its own.
      const copy = failureCopy(state.error);
      note(control, panel, `${copy.title}. ${copy.detail}`);
      return;
    }
    case "empty":
      note(
        control,
        panel,
        "Todavía no hay nadie en el equipo a quien asignar esto.",
      );
      return;
    case "ready":
      for (const member of state.members) {
        option(control, panel, {
          alias: member.alias,
          label: member.label,
          meta: loadMeta(member.open_tasks, member.is_overloaded),
        });
      }
      option(control, panel, {
        alias: "",
        label: UNASSIGNED,
        meta: "Deja el trabajo sin dueño",
      });
      return;
    default:
      return assertNever(state);
  }
}

/**
 * Wires every owner control inside `container`.
 *
 * @param container - Delegation root; `[data-project-header]` or `[data-tasks]`.
 * @param submit - Performs the write for the aggregate the control names.
 * @returns The teardown.
 */
export function mountOwnerMenus(
  container: HTMLElement,
  submit: OwnerSubmit,
): () => void {
  return mountMenus(container, "[data-owner-control]", {
    fill: fillPanel,
    choose: async (control, item) => {
      const alias = item.dataset.alias ?? "";
      // Re-choosing the current owner is not an edit; a PATCH here would be a
      // no-op the server accepts and the timeline would rather not record.
      if ((control.dataset.current ?? "") === alias) return;

      const trigger = control.querySelector<HTMLButtonElement>(
        "[data-menu-trigger]",
      );
      if (trigger !== null) setPending(trigger, true);
      try {
        await submit(control, alias === "" ? null : alias);
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
