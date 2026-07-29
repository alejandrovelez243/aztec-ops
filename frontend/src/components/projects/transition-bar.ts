/**
 * Moving a project through its workflow — and the one place on this surface
 * where optimism is banned.
 *
 * Legality lives in `WorkflowTransition` rows, guards and `requires_fields`;
 * the client holds none of it. So the button enters a pending state, the POST
 * goes out, and **nothing is repainted until the `project.state_changed`
 * envelope for this project arrives**: the stream is the authority, and a
 * rollback after the operator has already read the new state is worse than a
 * wait (`docs/standards/PATTERNS_FRONTEND.md` §8).
 *
 * The one concession to reality is {@link STREAM_CONFIRM_MS}: if the stream is
 * down, the button would otherwise spin forever on a move the server already
 * accepted. Past the deadline the surface paints the response — which is the
 * server's own answer, not a guess — and says out loud that the live
 * confirmation never came.
 *
 * The same handler serves remote moves: an envelope with no pending request of
 * ours means somebody else moved this project, and the surface re-reads it
 * (the payload carries state *codes*, never the labels and colours a chip
 * needs) and announces who did it.
 */

import { getProject, postProjectTransition } from "../../lib/api/client";
import { shake } from "../../lib/motion/spring";
import { subscribe } from "../../lib/stream/store";
import { toast } from "../../lib/toast";
import {
  applyTone,
  cloneTemplate,
  isFresher,
  pulse,
  setField,
  setPending,
} from "./dom";
import { dueState } from "./format";
import { failureCopy, joinFields, riskLabel } from "./messages";
import { emptyAggregateFields } from "./presentation";
import { markEvent } from "./stale";
import { healthTone, semanticTone, severityTone, stateTone } from "./tone";
import type { ProjectDetail } from "../../lib/api/domain";

/** How long the pending button waits for the stream before trusting the POST. */
const STREAM_CONFIRM_MS = 8_000;

/** A move the server has accepted and the stream has not yet confirmed. */
interface PendingMove {
  readonly button: HTMLButtonElement;
  readonly project: ProjectDetail;
  readonly timer: number;
}

/**
 * Mounts the transition bar.
 *
 * @param bar - The element carrying `data-transition-bar`.
 * @returns The teardown; drops the stream subscription and any pending timer.
 */
export function mountTransitionBar(bar: HTMLElement): () => void {
  const code = bar.dataset.projectCode ?? "";
  const controller = new AbortController();
  const { signal } = controller;
  const dialog = bar.querySelector<HTMLDialogElement>("[data-reason-dialog]");
  const form = bar.querySelector<HTMLFormElement>("[data-reason-form]");

  let pending: PendingMove | null = null;
  let awaitingReason: HTMLButtonElement | null = null;

  /** Paints the answer and releases the button, once. */
  const settle = (source: "stream" | "timeout"): void => {
    if (pending === null) return;
    const move = pending;
    pending = null;
    window.clearTimeout(move.timer);
    setPending(move.button, false);
    applyProject(bar, move.project);
    if (source === "stream") {
      toast({
        kind: "success",
        title: "Estado actualizado",
        detail: `El proyecto quedó en ${move.project.state.label}.`,
      });
      return;
    }
    toast({
      kind: "info",
      title: "Movimiento guardado sin confirmación en vivo",
      detail:
        "El servidor aceptó el cambio, pero el stream no lo confirmó. Reconecta para volver a ver los cambios de tus colegas.",
    });
  };

  const send = async (button: HTMLButtonElement, reason: string): Promise<void> => {
    setPending(button, true);
    const result = await postProjectTransition(code, {
      to_state: button.dataset.toState ?? "",
      reason,
    });

    if (!result.ok) {
      setPending(button, false);
      void shake(button);
      const copy = failureCopy(result.error);
      toast({ kind: "error", title: copy.title, detail: copy.detail });
      // A refused move means this view was stale about legality, which is a
      // disagreement about *shape*: re-read instead of patching.
      if (result.error.kind === "transition_not_allowed") void resync(bar, code);
      return;
    }

    pending = {
      button,
      project: result.data,
      timer: window.setTimeout(() => {
        settle("timeout");
      }, STREAM_CONFIRM_MS),
    };
  };

  bar.addEventListener(
    "click",
    (event) => {
      const target = event.target;
      if (!(target instanceof Element)) return;

      if (target.closest("[data-action='close-reason']") !== null) {
        dialog?.close();
        awaitingReason = null;
        return;
      }

      const button = target.closest<HTMLButtonElement>("[data-transition]");
      if (button === null || button.disabled) return;

      if (button.dataset.requiresReason === "true") {
        awaitingReason = button;
        setField(bar, "reason-target", button.textContent?.trim() ?? "");
        dialog?.showModal();
        return;
      }
      void send(button, "");
    },
    { signal },
  );

  form?.addEventListener(
    "submit",
    (event) => {
      event.preventDefault();
      const reason = String(new FormData(form).get("reason") ?? "").trim();
      const errorLine = bar.querySelector<HTMLElement>("[data-field='reason-error']");
      if (reason === "") {
        if (errorLine !== null) {
          errorLine.hidden = false;
          errorLine.textContent = "Esta transición no se puede registrar sin un motivo.";
        }
        return;
      }
      if (errorLine !== null) errorLine.hidden = true;
      const button = awaitingReason;
      awaitingReason = null;
      dialog?.close();
      form.reset();
      if (button !== null) void send(button, reason);
    },
    { signal },
  );

  const off = subscribe("project.state_changed", (envelope) => {
    if (envelope.entity.id !== code) return;
    markEvent(envelope.occurred_at);
    if (pending !== null) {
      settle("stream");
      return;
    }
    if (!isFresher(envelope.occurred_at, bar.dataset.updatedAt)) return;
    void announceRemote(bar, code, envelope.actor);
  });

  return () => {
    controller.abort();
    off();
    if (pending !== null) window.clearTimeout(pending.timer);
  };
}

/** Re-reads the project after a refusal, so the buttons match reality again. */
async function resync(bar: HTMLElement, code: string): Promise<void> {
  const result = await getProject(code);
  if (!result.ok) return;
  applyProject(bar, result.data);
}

/**
 * Repaints after somebody else's move and names them.
 *
 * The attribution matters: DESIGN.md's remote-move signature is that a
 * colleague's change is *shown* to have a hand behind it, never silently
 * appearing.
 */
async function announceRemote(
  bar: HTMLElement,
  code: string,
  actor: string,
): Promise<void> {
  const result = await getProject(code);
  if (!result.ok) return;
  applyProject(bar, result.data);
  toast({
    kind: "info",
    title: `${actor} movió este proyecto`,
    detail: `Ahora está en ${result.data.state.label}.`,
  });
}

/**
 * Paints one authoritative project read across the header, the bar and the
 * risk list.
 *
 * Field-level patches against the rendered DOM, never a rebuild of the page:
 * the operator may be halfway through a form in another tab of this detail,
 * and replacing the region would take it away from them.
 */
function applyProject(bar: HTMLElement, project: ProjectDetail): void {
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

    const dueChip = header.querySelector<HTMLElement>("[data-due-chip]");
    if (dueChip !== null) {
      const due = dueState(project.target_date ?? null);
      applyTone(dueChip, semanticTone(due.tone));
      setField(dueChip, "due-label", due.label);
      if ("title" in due) dueChip.setAttribute("title", due.title);
      else dueChip.removeAttribute("title");
    }

    patchNextStep(header, project.next_step ?? null);
    header.dataset.updatedAt = project.updated_at;
    pulse(header);
  }

  rebuildOptions(bar, project);
  bar.dataset.updatedAt = project.updated_at;
  patchRiskFlags(project);
}

/** The next step, or the ámbar chip that names its absence. */
function patchNextStep(header: HTMLElement, nextStep: string | null): void {
  const slot = header.querySelector<HTMLElement>("[data-next-step]");
  if (slot === null) return;
  const isAbsent = nextStep === null || nextStep === "";
  const node = document.createElement("span");
  node.className = isAbsent ? "chip tone-ambar" : "body-text";
  node.textContent = isAbsent ? "Sin próximo paso" : nextStep;
  slot.replaceChildren(node);
}

/** Rebuilds the buttons from the fresh list of legal moves. */
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

    const missing = transition.requires_fields.filter((field) => empty.has(field));
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
 * Rewrites the risk list: a state change can raise or clear flags, and they are
 * computed on read (ADR 0011) rather than announced by an event of their own.
 */
function patchRiskFlags(project: ProjectDetail): void {
  const list = document.querySelector<HTMLElement>("[data-risk-list]");
  if (list === null) return;
  const items = list.querySelector<HTMLElement>("[data-risk-items]");
  const none = list.querySelector<HTMLElement>("[data-risk-none]");
  if (items === null || none === null) return;

  const flags = project.risk_flags;
  none.hidden = flags.length > 0;
  items.hidden = flags.length === 0;
  items.replaceChildren();

  for (const flag of flags) {
    const item = cloneTemplate(list, "risk");
    if (item === null) continue;
    applyTone(item, semanticTone(severityTone(flag.severity)));
    setField(item, "risk-name", riskLabel(flag.code));
    setField(item, "risk-reason", flag.reason);
    items.appendChild(item);
  }
}
