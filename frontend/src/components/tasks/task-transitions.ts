/**
 * Moving a task through its workflow — and, exactly as on the project screen,
 * the one place on this surface where optimism is banned.
 *
 * Legality lives in `WorkflowTransition` rows, guards and `requires_fields`; the
 * client holds none of it. So the button enters a pending state, the POST goes
 * out, and **nothing is repainted until the `task.state_changed` envelope for
 * this task arrives**: the stream is the authority, and a rollback after the
 * operator has already read the new state is worse than a wait
 * (`docs/standards/PATTERNS_FRONTEND.md` §8).
 *
 * The confirmation is followed by a re-read rather than by painting the POST's
 * answer, and that is deliberate: `POST /tasks/{code}/transition` responds with
 * the narrow `TaskView`, which carries the new state but **not** the moves legal
 * from it. Painting that alone would leave the operator looking at the buttons
 * of the state they just left. The state label is taken from the response all
 * the same, so the toast can name where the task landed even if the re-read is
 * the request that fails.
 *
 * The one concession to reality is {@link STREAM_CONFIRM_MS}: with the stream
 * down the button would otherwise spin forever on a move the server already
 * accepted. Past the deadline the surface re-reads — which is the server's own
 * answer, not a guess — and says out loud that the live confirmation never came.
 *
 * The same handler serves remote moves: an envelope with no pending request of
 * ours means somebody else moved this task, and the surface re-reads it (the
 * payload carries state *codes*, never the labels and colours a chip needs) and
 * announces who did it.
 */

import { getTask, postTaskTransition } from "../../lib/api/client";
import { shake } from "../../lib/motion/spring";
import { subscribe } from "../../lib/stream/store";
import { toast } from "../../lib/toast";
import { isFresher, setPending } from "../projects/dom";
import { failureCopy } from "../projects/messages";
import { askReason } from "../projects/reason-dialog";
import { markEvent } from "../projects/stale";
import { applyTaskDetail } from "./task-paint";

/** How long the pending button waits for the stream before trusting the POST. */
const STREAM_CONFIRM_MS = 8_000;

/** A move the server has accepted and the stream has not yet confirmed. */
interface PendingMove {
  readonly button: HTMLButtonElement;
  /** Where the server says the task landed; the words of the toast. */
  readonly stateLabel: string;
  readonly timer: number;
}

/**
 * Mounts the task transition bar.
 *
 * @param bar - The element carrying `data-task-transition-bar`.
 * @returns The teardown; drops the stream subscription and any pending timer.
 */
export function mountTaskTransitions(bar: HTMLElement): () => void {
  const code = bar.dataset.taskCode ?? "";
  const controller = new AbortController();
  const { signal } = controller;

  let pending: PendingMove | null = null;

  /** Releases the button and re-reads the task, once. */
  const settle = (source: "stream" | "timeout"): void => {
    if (pending === null) return;
    const move = pending;
    pending = null;
    window.clearTimeout(move.timer);
    setPending(move.button, false);

    if (source === "stream") {
      toast({
        kind: "success",
        title: "Estado actualizado",
        detail: `La tarea quedó en ${move.stateLabel}.`,
      });
    } else {
      toast({
        kind: "info",
        title: "Movimiento guardado sin confirmación en vivo",
        detail:
          "El servidor aceptó el cambio, pero el stream no lo confirmó. Reconecta para volver a ver los cambios de tus colegas.",
      });
    }
    void resync(code);
  };

  const send = async (
    button: HTMLButtonElement,
    reason: string,
  ): Promise<void> => {
    setPending(button, true);
    const result = await postTaskTransition(code, {
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
      if (result.error.kind === "transition_not_allowed") void resync(code);
      return;
    }

    pending = {
      button,
      stateLabel: result.data.state.label,
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
      const button = target.closest<HTMLButtonElement>(
        "[data-task-transition]",
      );
      if (button === null || button.disabled) return;
      void submit(button);
    },
    { signal },
  );

  const off = subscribe("task.state_changed", (envelope) => {
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

/**
 * Re-reads the task and paints the answer.
 *
 * A failure here leaves the screen showing what it showed before, which is now
 * known to be behind the server — so it is said out loud rather than left for
 * the operator to discover by acting on it.
 */
async function resync(code: string): Promise<void> {
  const result = await getTask(code);
  if (!result.ok) {
    const copy = failureCopy(result.error);
    toast({
      kind: "error",
      title: "No pudimos releer la tarea",
      detail: `${copy.detail} Recarga la página para ver su estado actual.`,
    });
    return;
  }
  // `applyTaskDetail` carries the new `updated_at` onto the header and the bar,
  // which is what makes the next envelope's freshness check meaningful.
  applyTaskDetail(result.data);
}

/**
 * Repaints after somebody else's move and names them.
 *
 * The attribution matters: DESIGN.md's remote-move signature is that a
 * colleague's change is *shown* to have a hand behind it, never silently
 * appearing.
 */
async function announceRemote(code: string, actor: string): Promise<void> {
  const result = await getTask(code);
  if (!result.ok) return;
  applyTaskDetail(result.data);
  toast({
    kind: "info",
    title: `${actor} movió esta tarea`,
    detail: `Ahora está en ${result.data.state.label}.`,
  });
}
