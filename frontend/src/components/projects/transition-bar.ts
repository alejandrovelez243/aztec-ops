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
 *
 * Two things this module used to own and no longer does: the motive dialog,
 * which is now `reason-dialog.ts` because a task's move can demand one too, and
 * the painting of a project read, which is now `project-paint.ts` because three
 * writers on this screen end with exactly that job.
 */

import { getProject, postProjectTransition } from "../../lib/api/client";
import { shake } from "../../lib/motion/spring";
import { subscribe } from "../../lib/stream/store";
import { toast } from "../../lib/toast";
import { isFresher, setPending } from "./dom";
import { failureCopy } from "./messages";
import { applyProjectDetail } from "./project-paint";
import { askReason } from "./reason-dialog";
import { markEvent } from "./stale";
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

  let pending: PendingMove | null = null;

  /** Paints the answer and releases the button, once. */
  const settle = (source: "stream" | "timeout"): void => {
    if (pending === null) return;
    const move = pending;
    pending = null;
    window.clearTimeout(move.timer);
    setPending(move.button, false);
    applyProjectDetail(move.project);
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

  const send = async (
    button: HTMLButtonElement,
    reason: string,
  ): Promise<void> => {
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
      if (result.error.kind === "transition_not_allowed")
        void resync(bar, code);
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

  /** Asks for a motive when the edge demands one, then submits. */
  const submit = async (button: HTMLButtonElement): Promise<void> => {
    if (button.dataset.requiresReason !== "true") {
      await send(button, "");
      return;
    }
    const reason = await askReason(button.textContent?.trim() ?? "");
    // A cancelled dialog is a cancelled move, not a move without a motive.
    if (reason === null) return;
    await send(button, reason);
  };

  bar.addEventListener(
    "click",
    (event) => {
      const target = event.target;
      if (!(target instanceof Element)) return;
      const button = target.closest<HTMLButtonElement>("[data-transition]");
      if (button === null || button.disabled) return;
      void submit(button);
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
    void announceRemote(code, envelope.actor);
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
  applyProjectDetail(result.data);
  bar.dataset.updatedAt = result.data.updated_at;
}

/**
 * Repaints after somebody else's move and names them.
 *
 * The attribution matters: DESIGN.md's remote-move signature is that a
 * colleague's change is *shown* to have a hand behind it, never silently
 * appearing.
 */
async function announceRemote(code: string, actor: string): Promise<void> {
  const result = await getProject(code);
  if (!result.ok) return;
  applyProjectDetail(result.data);
  toast({
    kind: "info",
    title: `${actor} movió este proyecto`,
    detail: `Ahora está en ${result.data.state.label}.`,
  });
}
