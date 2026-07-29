/**
 * Painting one authoritative project read across the detail screen.
 *
 * Every writer on this surface — the transition bar, the next-step editor, the
 * owner picker — ends the same way: it holds a `ProjectDetailView` the server
 * produced *after* the write and has to make the whole screen agree with it.
 * That job lives here once. Written per island, the three would drift, and the
 * risk list is exactly where drift is expensive: flags are computed on read
 * (ADR 0011), so they change as a *consequence* of edits that never mention
 * them, and an island that forgot to repaint them leaves a cured risk on screen.
 *
 * Field-level patches against the rendered DOM, never a rebuild of the region:
 * the operator may be halfway through the blocker form in another tab of this
 * detail, and replacing the region would take it away from them.
 */

import { applyTone, cloneTemplate, pulse, setField } from "./dom";
import { renderDateControl } from "../ui/date-field";
import { renderLifecyclePlate } from "../lifecycle/paint";
import { joinFields, riskLabel } from "./messages";
import { renderOwnerControl } from "./owner-menu";
import { emptyAggregateFields } from "./presentation";
import { healthTone, semanticTone, severityTone, stateTone } from "./tone";
import type { ProjectDetail } from "../../lib/api/domain";

/**
 * Paints a project read across the header, the transition bar and the risks.
 *
 * @param project - The server's answer; the only source of every value written.
 */
export function applyProjectDetail(project: ProjectDetail): void {
  const header = document.querySelector<HTMLElement>("[data-project-header]");
  if (header !== null) {
    const chip = header.querySelector<HTMLElement>("[data-state-chip]");
    if (chip !== null) {
      applyTone(chip, stateTone(project.state));
      setField(chip, "state-label", project.state.label);
      chip.dataset.category = project.state.category;
    }

    const health = header.querySelector<HTMLElement>("[data-health-chip]");
    if (health !== null) {
      applyTone(health, semanticTone(healthTone(project.health.code)));
      health.textContent = project.health.label;
    }

    // The target date is a control now, not a chip: one painter, so a date this
    // screen set and one a colleague set leave it reading the same thing.
    const dueControl = header.querySelector<HTMLElement>("[data-date-control]");
    if (dueControl !== null) {
      renderDateControl(dueControl, project.target_date ?? null);
    }

    const owner = header.querySelector<HTMLElement>("[data-owner-control]");
    if (owner !== null) renderOwnerControl(owner, project.owner ?? null);

    renderNextStep(header, project.next_step ?? null);
    // The lifecycle and the state travel together on purpose: which graphs this
    // project could be moved onto depends on the state it is standing on, so a
    // transition has to reach the plate even though it changes no lifecycle.
    renderLifecyclePlate(header, project);
    header.dataset.updatedAt = project.updated_at;
    pulse(header);
  }

  const bar = document.querySelector<HTMLElement>("[data-transition-bar]");
  if (bar !== null) {
    rebuildOptions(bar, project);
    bar.dataset.updatedAt = project.updated_at;
  }

  patchRiskFlags(project);
}

/**
 * Writes the next step into its field, in whichever arm it belongs.
 *
 * `data-filled` is the field's single mode flag: the "escríbelo" invitation and
 * the written sentence are two renderings of one value, so they are never both
 * shown and never both hidden. The input is primed with the same text, because
 * the operator who opens the editor next is editing what is on screen — not what
 * was on screen when the page was built.
 */
export function renderNextStep(
  scope: ParentNode,
  nextStep: string | null,
): void {
  const field = scope.querySelector<HTMLElement>("[data-next-step-field]");
  if (field === null) return;
  const value = nextStep ?? "";
  field.dataset.filled = value === "" ? "false" : "true";
  setField(field, "next-step-value", value);
  const input = field.querySelector<HTMLInputElement>("[data-next-step-input]");
  if (input !== null) input.value = value;
}

/** Rebuilds the transition buttons from the fresh list of legal moves. */
function rebuildOptions(bar: HTMLElement, project: ProjectDetail): void {
  const list = bar.querySelector<HTMLElement>("[data-transition-options]");
  if (list === null) return;

  const empty = new Set(emptyAggregateFields(project));
  list.replaceChildren();

  for (const transition of project.transitions) {
    const option = cloneTemplate(bar, "transition");
    if (option === null) continue;
    const button = option.querySelector<HTMLButtonElement>("[data-transition]");
    const missingLine = option.querySelector<HTMLElement>(
      "[data-field='transition-missing']",
    );
    if (button === null) continue;

    button.dataset.toState = transition.to_state.code;
    button.dataset.requiresReason = String(transition.requires_reason);
    setField(option, "transition-label", transition.label);

    const missing = transition.requires_fields.filter((field) =>
      empty.has(field),
    );
    if (missing.length > 0 && missingLine !== null) {
      const id = `req-${project.code}-${transition.to_state.code}`;
      button.disabled = true;
      button.setAttribute("aria-describedby", id);
      missingLine.id = id;
      missingLine.hidden = false;
      missingLine.textContent = `Falta ${joinFields(missing)}`;
    }
    list.appendChild(option);
  }

  const terminal = bar.querySelector<HTMLElement>("[data-no-transitions]");
  if (terminal !== null) terminal.hidden = project.transitions.length > 0;
}

/**
 * Rewrites the risk list, and makes a change to it *visible*.
 *
 * Flags are computed on read (ADR 0011) rather than announced by an event of
 * their own, so the only moment a cure is observable is the read that follows
 * it. The list pulses when the set of raised codes actually differs from what
 * is on screen — which is what turns "escribí el próximo paso" into "y el
 * riesgo se fue", one gesture, one answer.
 */
function patchRiskFlags(project: ProjectDetail): void {
  const list = document.querySelector<HTMLElement>("[data-risk-list]");
  if (list === null) return;
  const items = list.querySelector<HTMLElement>("[data-risk-items]");
  const none = list.querySelector<HTMLElement>("[data-risk-none]");
  if (items === null || none === null) return;

  const flags = project.risk_flags;
  const codes = flags.map((flag) => flag.code).join(" ");
  const changed = (items.dataset.codes ?? "") !== codes;

  none.hidden = flags.length > 0;
  items.hidden = flags.length === 0;
  items.dataset.codes = codes;
  items.replaceChildren();

  for (const flag of flags) {
    const item = cloneTemplate(list, "risk");
    if (item === null) continue;
    applyTone(item, semanticTone(severityTone(flag.severity)));
    setField(item, "risk-name", riskLabel(flag));
    setField(item, "risk-reason", flag.reason);
    items.appendChild(item);
  }

  if (changed) pulse(list);
}

/** Whether the project currently raises the flag with this code. */
export function hasRisk(project: ProjectDetail, code: string): boolean {
  return project.risk_flags.some((flag) => flag.code === code);
}
