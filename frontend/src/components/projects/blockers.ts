/**
 * Raising and clearing blockers, and keeping the panel true while colleagues do
 * the same.
 *
 * Both lists are rebuilt from a **fresh read** rather than patched from the
 * envelope, for one concrete reason: `blocker.raised` carries the blocker's
 * business code, while `POST /api/v1/blockers/{id}/resolve` is addressed by the
 * numeric primary key. A row built from a payload would render a "Resolver"
 * button that has no id to send — which is exactly the class of bug that
 * patching a *shape* instead of a *value* produces
 * (`docs/standards/PATTERNS_FRONTEND.md` §6).
 *
 * The rebuild is cheap (a handful of rows) and always correct, and every row it
 * writes is cloned from the panel's own templates, so the live list and the
 * server-rendered one cannot drift apart.
 */

import {
  getProject,
  postBlockerResolve,
  postProjectBlocker,
} from "../../lib/api/client";
import { subscribe } from "../../lib/stream/store";
import type { Topic } from "../../lib/stream/topics";
import { toast } from "../../lib/toast";
import { cloneTemplate, readString, setField, setPending } from "./dom";
import { formatInstant } from "./format";
import { blockerKindLabel, failureCopy } from "./messages";
import { blockerMeta, resolvedBlockerMeta } from "./presentation";
import { askBlocker } from "./raise-blocker-dialog";
import { markEvent } from "./stale";
import type { Blocker } from "../../lib/api/domain";

/** Topics that change what this panel shows. */
const WATCHED: readonly Topic[] = ["blocker.raised", "blocker.resolved"];

/**
 * Mounts the blockers panel.
 *
 * @param root - The element carrying `data-blockers`.
 * @returns The teardown; drops the subscriptions and the listeners.
 */
export function mountBlockers(root: HTMLElement): () => void {
  const code = root.dataset.projectCode ?? "";
  const controller = new AbortController();
  const { signal } = controller;

  const dialog = root.querySelector<HTMLDialogElement>("[data-resolve-dialog]");
  const resolveForm = root.querySelector<HTMLFormElement>(
    "[data-resolve-form]",
  );

  /** Which blocker the open dialog is about; the numeric key the API wants. */
  let target: number | null = null;

  root.addEventListener(
    "click",
    (event) => {
      const element = event.target;
      if (!(element instanceof Element)) return;

      if (element.closest("[data-action='open-raise']") !== null) {
        void raise(root, code);
        return;
      }
      if (element.closest("[data-action='close-resolve']") !== null) {
        dialog?.close();
        target = null;
        return;
      }
      if (element.closest("[data-action='resolve']") === null) return;

      const item = element.closest<HTMLElement>("[data-blocker]");
      const id = Number(item?.dataset.blockerId);
      if (!Number.isFinite(id)) return;
      target = id;
      dialog?.showModal();
    },
    { signal },
  );

  resolveForm?.addEventListener(
    "submit",
    (event) => {
      event.preventDefault();
      const resolution = String(
        new FormData(resolveForm).get("resolution") ?? "",
      ).trim();
      const errorLine = root.querySelector<HTMLElement>(
        "[data-field='resolve-error']",
      );
      if (resolution === "" || target === null) {
        if (errorLine !== null) {
          errorLine.hidden = false;
          errorLine.textContent =
            "Describe cómo se destrabó antes de cerrarlo.";
        }
        return;
      }
      if (errorLine !== null) errorLine.hidden = true;
      const id = target;
      target = null;
      void resolve(root, code, id, resolution, resolveForm, dialog);
    },
    { signal },
  );

  const offs = WATCHED.map((topic) =>
    subscribe(topic, (envelope) => {
      if (readString(envelope.payload, "project_code") !== code) return;
      markEvent(envelope.occurred_at);
      void refresh(root, code);
    }),
  );

  return () => {
    controller.abort();
    for (const off of offs) off();
  };
}

/** Sends the resolution and lets the fresh read repaint both lists. */
async function resolve(
  root: HTMLElement,
  code: string,
  id: number,
  resolution: string,
  form: HTMLFormElement,
  dialog: HTMLDialogElement | null,
): Promise<void> {
  const button = form.querySelector<HTMLButtonElement>(
    "[data-action='submit-resolve']",
  );
  if (button !== null) setPending(button, true);

  const result = await postBlockerResolve(id, { resolution });

  if (button !== null) setPending(button, false);
  if (!result.ok) {
    const copy = failureCopy(result.error);
    toast({ kind: "error", title: copy.title, detail: copy.detail });
    return;
  }

  dialog?.close();
  form.reset();
  await refresh(root, code);
  toast({
    kind: "success",
    title: "Bloqueo resuelto",
    detail:
      "El motor recalcula la prioridad del proyecto en cuanto lo procesa.",
  });
}

/**
 * Asks for a blocker and raises it against the project.
 *
 * The kind and the description are validated inside the dialog, which is the
 * only place the operator can still fix them; by the time this resumes, the
 * draft is one the API accepts and the modal is gone. The in-flight state
 * therefore lands on the button that opened it — the only control still on
 * screen — because a surface that goes quiet after a click reads as broken.
 */
async function raise(root: HTMLElement, code: string): Promise<void> {
  const draft = await askBlocker(root);
  if (draft === null) return;

  const button = root.querySelector<HTMLButtonElement>(
    "[data-action='open-raise']",
  );
  if (button !== null) setPending(button, true);

  const result = await postProjectBlocker(code, {
    kind: draft.kind,
    description: draft.description,
  });

  if (button !== null) setPending(button, false);
  if (!result.ok) {
    const copy = failureCopy(result.error);
    toast({ kind: "error", title: copy.title, detail: copy.detail });
    return;
  }

  await refresh(root, code);
  toast({
    kind: "success",
    title: "Bloqueo registrado",
    detail: "Aparece en el panel y pesa en la prioridad del proyecto.",
  });
}

/** Re-reads the project and rebuilds both lists from the panel's templates. */
async function refresh(root: HTMLElement, code: string): Promise<void> {
  const result = await getProject(code);
  if (!result.ok) return;
  render(root, result.data.blockers);
}

/** Writes the two lists, their counts and the "nothing is blocked" chip. */
function render(root: HTMLElement, blockers: readonly Blocker[]): void {
  const open = blockers.filter(
    (blocker) => (blocker.resolved_at ?? null) === null,
  );
  const resolved = blockers.filter(
    (blocker) => (blocker.resolved_at ?? null) !== null,
  );

  fill(root, "[data-open-blockers]", "open-blocker", open, false);
  fill(root, "[data-resolved-blockers]", "resolved-blocker", resolved, true);

  const empty = root.querySelector<HTMLElement>("[data-open-empty]");
  if (empty !== null) empty.hidden = open.length > 0;

  setField(root, "resolved-count", String(resolved.length));
  const tabs = document.querySelector<HTMLElement>("[data-tabs]");
  if (tabs !== null) setField(tabs, "blockers-count", String(open.length));
}

/** Clones one template per blocker into the named list. */
function fill(
  root: HTMLElement,
  selector: string,
  template: string,
  blockers: readonly Blocker[],
  isResolved: boolean,
): void {
  const list = root.querySelector<HTMLElement>(selector);
  if (list === null) return;
  list.replaceChildren();

  for (const blocker of blockers) {
    const item = cloneTemplate(root, template);
    if (item === null) continue;
    item.dataset.blockerId = String(blocker.id);
    setField(item, "blocker-kind", blockerKindLabel(blocker.kind));
    setField(item, "blocker-code", blocker.code);
    setField(item, "blocker-description", blocker.description);
    setField(
      item,
      "blocker-meta",
      isResolved ? resolvedBlockerMeta(blocker) : blockerMeta(blocker),
    );
    if (isResolved) {
      setField(item, "blocker-resolution", blocker.resolution_reason);
    }

    const taskChip = item.querySelector<HTMLElement>(
      "[data-field='blocker-task']",
    );
    const taskCode = blocker.task_code ?? null;
    if (taskChip !== null) {
      taskChip.hidden = taskCode === null;
      taskChip.textContent = taskCode ?? "";
    }

    const stamp = isResolved
      ? (blocker.resolved_at ?? null)
      : blocker.raised_at;
    const absolute = formatInstant(stamp);
    const meta = item.querySelector<HTMLElement>("[data-field='blocker-meta']");
    if (meta !== null && absolute !== null) meta.title = absolute;

    list.appendChild(item);
  }
}
