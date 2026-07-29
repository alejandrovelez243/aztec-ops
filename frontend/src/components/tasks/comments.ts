/**
 * Writing comments on a task, and receiving the ones colleagues write.
 *
 * This is the region where the *payload is the entity*: `note.added` carries the
 * body and the author (`docs/EVENTS.md` §4), a comment is immutable once
 * written, and re-reading the task to learn one sentence would be a round trip
 * for information already in hand. So a remote comment is rendered straight from
 * the envelope — with the envelope's `occurred_at` as its timestamp, never the
 * browser's clock, which would date a colleague's comment by when this tab
 * happened to receive it.
 *
 * The write goes to the project's route with `task_code` set, which is the only
 * way the API accepts a task comment; the task screen never invents a route of
 * its own.
 *
 * Deduplication is by comment code: delivery is at-least-once, and the
 * operator's own comment arrives twice — once as the POST's response, once as
 * the envelope their own write produced.
 */

import { postProjectNote } from "../../lib/api/client";
import { subscribe } from "../../lib/stream/store";
import { toast } from "../../lib/toast";
import {
  cloneTemplate,
  readString,
  setField,
  setPending,
} from "../projects/dom";
import { formatInstant, relativeTime } from "../projects/format";
import { failureCopy } from "../projects/messages";
import { markEvent } from "../projects/stale";

/** What an empty submission is answered with, in the form itself. */
const EMPTY_BODY = "Escribe el comentario antes de publicarlo.";

/** One comment as either source delivers it. */
interface CommentRow {
  readonly code: string;
  readonly author: string;
  readonly body: string;
  readonly occurredAt: string;
}

/**
 * Mounts the comments panel.
 *
 * @param root - The element carrying `data-comments`.
 * @returns The teardown; drops the subscription and the form listener.
 */
export function mountComments(root: HTMLElement): () => void {
  const taskCode = root.dataset.taskCode ?? "";
  const projectCode = root.dataset.projectCode ?? "";
  const controller = new AbortController();
  const form = root.querySelector<HTMLFormElement>("[data-comment-form]");

  form?.addEventListener(
    "submit",
    (event) => {
      event.preventDefault();
      void publish(root, { projectCode, taskCode, form });
    },
    { signal: controller.signal },
  );

  const off = subscribe("note.added", (envelope) => {
    // A project-wide note carries `task_code: null` and belongs to the project
    // timeline, not to this task's conversation.
    if (readString(envelope.payload, "task_code") !== taskCode) return;
    markEvent(envelope.occurred_at);
    prepend(root, {
      code: envelope.entity.id,
      author: readString(envelope.payload, "author_alias") ?? envelope.actor,
      body: readString(envelope.payload, "body") ?? "",
      occurredAt: envelope.occurred_at,
    });
  });

  return () => {
    controller.abort();
    off();
  };
}

/** Everything one submission needs; grouped so the signature stays readable. */
interface PublishTarget {
  readonly projectCode: string;
  readonly taskCode: string;
  readonly form: HTMLFormElement;
}

/** Sends one comment and renders the row the server answered with. */
async function publish(
  root: HTMLElement,
  target: PublishTarget,
): Promise<void> {
  const { form } = target;
  const body = String(new FormData(form).get("body") ?? "").trim();
  const errorLine = root.querySelector<HTMLElement>(
    "[data-field='comment-error']",
  );

  if (body === "") {
    if (errorLine !== null) {
      errorLine.hidden = false;
      errorLine.textContent = EMPTY_BODY;
    }
    return;
  }
  if (errorLine !== null) errorLine.hidden = true;

  const button = form.querySelector<HTMLButtonElement>(
    "[data-action='add-comment']",
  );
  if (button !== null) setPending(button, true);

  const result = await postProjectNote(target.projectCode, {
    body,
    task_code: target.taskCode,
  });

  if (button !== null) setPending(button, false);
  if (!result.ok) {
    const copy = failureCopy(result.error);
    toast({ kind: "error", title: copy.title, detail: copy.detail });
    return;
  }

  form.reset();
  prepend(root, {
    code: result.data.code,
    author: result.data.author,
    body: result.data.body,
    occurredAt: result.data.created_at,
  });
  toast({
    kind: "success",
    title: "Comentario publicado",
    detail: "Queda en la bitácora de la tarea.",
  });
}

/** Adds one comment to the top of the list, unless it is already there. */
function prepend(root: HTMLElement, comment: CommentRow): void {
  const list = root.querySelector<HTMLElement>("[data-comment-list]");
  if (list === null) return;
  if (
    list.querySelector(
      `[data-comment][data-code="${CSS.escape(comment.code)}"]`,
    ) !== null
  ) {
    return;
  }

  const item = cloneTemplate(root, "comment");
  if (item === null) return;
  item.dataset.code = comment.code;
  setField(item, "comment-author", comment.author);
  setField(item, "comment-body", comment.body);
  setField(item, "comment-when", relativeTime(comment.occurredAt) ?? "");

  const when = item.querySelector<HTMLElement>("[data-field='comment-when']");
  const absolute = formatInstant(comment.occurredAt);
  if (when !== null && absolute !== null) when.title = absolute;

  list.prepend(item);

  const empty = root.querySelector<HTMLElement>("[data-comments-empty]");
  if (empty !== null) empty.hidden = true;
}
