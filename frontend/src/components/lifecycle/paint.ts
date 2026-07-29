/**
 * Writing one authoritative read into the lifecycle plate.
 *
 * Its own module rather than a function of the island, and the reason is a cycle: the project and
 * task painters have to write this plate — a reassignment repaints the whole record through them —
 * while the island calls those same painters when its write succeeds. With the painter living here,
 * beside the values it writes and importing neither of them, the dependency runs one way.
 *
 * Everything below is a field-level patch against markup the server rendered, never a rebuild: the
 * dialog may be open, the operator may be halfway through choosing, and replacing the region would
 * take that away from them.
 */

import { setField } from "../../lib/dom/patch";
import { scopeCopy, toScope, workflowHref } from "./presentation";
import type { StateRef, WorkflowRef } from "../../lib/api/domain";

/**
 * The two facts the plate renders, as both details publish them.
 *
 * `state` is here and not only `workflow` because the plate is what decides which lifecycles a
 * record may be moved onto, and that answer depends on the state it is standing on: a transition
 * changes which targets are compatible without changing the lifecycle at all.
 */
export interface LifecycleRecord {
  readonly workflow: WorkflowRef;
  readonly state: StateRef;
}

/**
 * Repaints the plate from an authoritative read.
 *
 * Called by both record painters, so a lifecycle changed here, one changed by a colleague, and a
 * state changed by anybody all leave this plate saying the same thing — including the state code
 * the next compatibility check compares against.
 *
 * @param scope - Where to look for the plate; a page that hosts none is a no-op, because this runs
 *   from painters that also serve screens without one.
 * @param record - The record as the server last described it; the only source of every value.
 */
export function renderLifecyclePlate(
  scope: ParentNode,
  record: LifecycleRecord,
): void {
  const plate = scope.querySelector<HTMLElement>("[data-lifecycle-plate]");
  if (plate === null) return;

  const inherited = record.workflow.source === "INHERITED";
  const copy = scopeCopy(toScope(plate.dataset.scope));

  setField(plate, "lifecycle-name", record.workflow.name);
  setField(
    plate,
    "lifecycle-note",
    inherited ? copy.inheritedNote : copy.directNote,
  );
  setField(plate, "lifecycle-state", record.state.label);

  const badge = plate.querySelector<HTMLElement>("[data-lifecycle-source]");
  if (badge !== null) {
    badge.textContent = inherited ? "Heredado" : "Asignado";
    badge.classList.toggle("tone-piedra", inherited);
    badge.classList.toggle("tone-cielo", !inherited);
  }

  // The way to the graph the record now follows, so "¿y cómo es ese flujo?" is
  // one click from the answer rather than a search on another screen.
  const link = plate.querySelector<HTMLAnchorElement>("[data-lifecycle-link]");
  if (link !== null) link.href = workflowHref(record.workflow.code);

  plate.dataset.workflowCode = record.workflow.code;
  plate.dataset.workflowSource = record.workflow.source;
  plate.dataset.stateCode = record.state.code;
  plate.dataset.stateLabel = record.state.label;
}
