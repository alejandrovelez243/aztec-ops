/**
 * The header's two moving parts: the owner it lets you change, and the edit
 * somebody else made to this task.
 *
 * They belong together because they are the same field seen from both sides — a
 * reassignment made here and one made from the project's task table land on the
 * same control, and it must end in the same state either way.
 *
 * `task.updated` names the task and the fields that moved, but its payload
 * carries *codes and raw values* — a priority code, an assignee code — while
 * the header renders labels, colours and an avatar. So the envelope is used for
 * what it actually proves (this task changed, at this instant, by this person)
 * and the values come from a re-read, which is also what keeps the derived
 * `is_overdue` honest.
 *
 * The state's own moves are not handled here: they arrive as
 * `task.state_changed` and belong to the island that may have one in flight
 * (`task-transitions.ts`). Two subscribers to that topic would settle the same
 * pending move twice.
 */

import { getTask } from "../../lib/api/client";
import { getOperator } from "../../lib/auth/session";
import { subscribe } from "../../lib/stream/store";
import { mountOwnerMenus } from "../projects/owner-menu";
import { mountDateFields } from "../ui/date-field";
import { submitTaskDueDate, submitTaskOwner } from "./task-writes";
import { toast } from "../../lib/toast";
import { isFresher } from "../projects/dom";
import { markEvent } from "../projects/stale";
import { applyTaskDetail } from "./task-paint";

/**
 * Mounts the task header.
 *
 * @param header - The element carrying `data-task-header`.
 * @returns The teardown; drops the stream subscription, so a header left behind
 *   by a navigation stops patching a detached screen.
 */
export function mountTaskHeader(header: HTMLElement): () => void {
  const code = header.dataset.taskCode ?? "";

  const offOwner = mountOwnerMenus(header, submitTaskOwner);
  const offDates = mountDateFields(header, submitTaskDueDate);

  const operator = getOperator();

  const offStream = subscribe("task.updated", (envelope) => {
    if (envelope.entity.id !== code) return;
    markEvent(envelope.occurred_at);
    if (!isFresher(envelope.occurred_at, header.dataset.updatedAt)) return;
    // An edit made on this screen comes back as its own envelope. Announcing it
    // would tell the operator that somebody updated the task they just updated,
    // on top of the writer's own confirmation. Their own moves are already
    // painted from the response, so the whole echo is dropped rather than
    // re-read silently — the same test the board applies before attributing a
    // card that moved.
    if (operator !== null && envelope.actor === operator.alias) return;
    void announce(code, envelope.actor);
  });

  return () => {
    offOwner();
    offDates();
    offStream();
  };
}

/**
 * Re-reads the task, paints it, and names the colleague who changed it.
 *
 * A silent repaint would be the one thing DESIGN.md's remote-move signature
 * forbids: the value under the operator's eyes changes and nothing says a hand
 * did it. A failed re-read stays quiet on purpose — the screen is unchanged and
 * still correct as of its own `updated_at`, and the stale banner already owns
 * the sentence about a connection that is not delivering.
 */
async function announce(code: string, actor: string): Promise<void> {
  const result = await getTask(code);
  if (!result.ok) return;
  applyTaskDetail(result.data);
  toast({
    kind: "info",
    title: `${actor} actualizó esta tarea`,
    detail: "Los datos en pantalla ya están al día.",
  });
}
