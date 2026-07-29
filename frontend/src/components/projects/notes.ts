/**
 * Publishing notes, and receiving the ones colleagues publish.
 *
 * The panel's existing notes are rendered by the server from the project
 * detail's `notes`; this island only ever *adds* to that list. It never clears
 * it and never re-reads it, so nothing here can make a persisted note vanish.
 *
 * This is the one region on the detail where the *payload is the entity*:
 * `note.added` carries the body and the author (`docs/EVENTS.md` §4) and a note
 * is immutable once written, so a remote note is rendered straight from the
 * envelope — with the envelope's `occurred_at` as its timestamp, never the
 * browser's clock, which would date a colleague's note by when this tab
 * happened to receive it.
 *
 * Deduplication is by note code: delivery is at-least-once, the operator's own
 * note arrives twice — once as the POST's response, once as the envelope their
 * own write produced — and a colleague's note may already be on the page,
 * because the server rendered it before this tab subscribed.
 */

import { postProjectNote } from "../../lib/api/client";
import { subscribe } from "../../lib/stream/store";
import { toast } from "../../lib/toast";
import { cloneTemplate, readString, setField, setPending } from "./dom";
import { formatInstant, relativeTime } from "./format";
import { failureCopy } from "./messages";
import { markEvent } from "./stale";

/** One note as either source delivers it. */
interface NoteRow {
  readonly code: string;
  readonly author: string;
  readonly body: string;
  readonly occurredAt: string;
}

/**
 * Mounts the notes panel.
 *
 * @param root - The element carrying `data-notes`.
 * @returns The teardown; drops the subscription and the form listener.
 */
export function mountNotes(root: HTMLElement): () => void {
  const code = root.dataset.projectCode ?? "";
  const controller = new AbortController();
  const form = root.querySelector<HTMLFormElement>("[data-note-form]");

  refreshRelativeTimes(root);

  form?.addEventListener(
    "submit",
    (event) => {
      event.preventDefault();
      void publish(root, code, form);
    },
    { signal: controller.signal },
  );

  const off = subscribe("note.added", (envelope) => {
    if (readString(envelope.payload, "project_code") !== code) return;
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

/**
 * Rewrites every server-rendered relative time against the current clock.
 *
 * The route is pre-rendered, so "hace 2 horas" was true when the build ran and
 * would otherwise be read as true now. The absolute instant in `title` is
 * already correct and is left alone.
 */
function refreshRelativeTimes(root: HTMLElement): void {
  for (const node of root.querySelectorAll<HTMLElement>("[data-occurred-at]")) {
    const occurredAt = node.dataset.occurredAt;
    if (occurredAt === undefined) continue;
    node.textContent = relativeTime(occurredAt) ?? "";
  }
}

/** Sends one note and renders the row the server answered with. */
async function publish(
  root: HTMLElement,
  code: string,
  form: HTMLFormElement,
): Promise<void> {
  const body = String(new FormData(form).get("body") ?? "").trim();
  const errorLine = root.querySelector<HTMLElement>(
    "[data-field='note-error']",
  );

  if (body === "") {
    if (errorLine !== null) {
      errorLine.hidden = false;
      errorLine.textContent = "Escribe la nota antes de publicarla.";
    }
    return;
  }
  if (errorLine !== null) errorLine.hidden = true;

  const button = form.querySelector<HTMLButtonElement>(
    "[data-action='add-note']",
  );
  if (button !== null) setPending(button, true);

  const result = await postProjectNote(code, { body });

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
    title: "Nota publicada",
    detail: "Queda en la bitácora del proyecto.",
  });
}

/** Adds one note to the top of the list, unless it is already there. */
function prepend(root: HTMLElement, note: NoteRow): void {
  const list = root.querySelector<HTMLElement>("[data-note-list]");
  if (list === null) return;
  if (
    list.querySelector(`[data-note][data-code="${CSS.escape(note.code)}"]`) !==
    null
  ) {
    return;
  }

  const item = cloneTemplate(root, "note");
  if (item === null) return;
  item.dataset.code = note.code;
  setField(item, "note-author", note.author);
  setField(item, "note-body", note.body);
  setField(item, "note-when", relativeTime(note.occurredAt) ?? "");

  const when = item.querySelector<HTMLElement>("[data-field='note-when']");
  if (when !== null) {
    // The same hook the server-rendered rows carry, so a clone is refreshed by
    // the next pass over the panel exactly like the rows it sits among.
    when.dataset.occurredAt = note.occurredAt;
    const absolute = formatInstant(note.occurredAt);
    if (absolute !== null) when.title = absolute;
  }

  list.prepend(item);

  const empty = root.querySelector<HTMLElement>("[data-notes-empty]");
  if (empty !== null) empty.hidden = true;
}
