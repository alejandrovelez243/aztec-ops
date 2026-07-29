/**
 * The timeline's two live behaviours: paging, and catching the records a change
 * writes while the page is open.
 *
 * An envelope is never rendered as a timeline row. The stream announces domain
 * events; the timeline shows `ActivityRecord`s, which are written in the same
 * transaction but are a different thing with a different vocabulary — the
 * verbs, the `from`/`to` values and the `correlation_id` that ties a
 * reprioritization together. So an envelope only says "re-read the first page",
 * and the records themselves come from the API.
 *
 * Relative times are re-rendered on mount because a server-rendered "hace 2
 * minutos" is as old as the page: on a surface people leave open all morning,
 * the timestamps would quietly become lies.
 */

import { getProjectActivity } from "../../lib/api/client";
import { subscribe } from "../../lib/stream/store";
import type { Topic } from "../../lib/stream/topics";
import { cloneTemplate, readString, setField, setPending } from "./dom";
import { formatInstant, relativeTime } from "./format";
import { verbLabel } from "./messages";
import { activityChange } from "./presentation";
import { markEvent } from "./stale";
import type { ActivityEntry } from "../../lib/api/domain";

/** Every topic that writes an `ActivityRecord` against a project. */
const WATCHED: readonly Topic[] = [
  "project.state_changed",
  "project.updated",
  "project.priority.recalculated",
  "blocker.raised",
  "blocker.resolved",
  "note.added",
  "task.created",
  "task.state_changed",
];

/**
 * Mounts the timeline.
 *
 * @param root - The element carrying `data-activity-feed`.
 * @returns The teardown; drops every subscription and the click listener.
 */
export function mountActivity(root: HTMLElement): () => void {
  const code = root.dataset.projectCode ?? "";
  const controller = new AbortController();

  refreshRelativeTimes(root);

  root.addEventListener(
    "click",
    (event) => {
      const target = event.target;
      if (!(target instanceof Element)) return;
      const button = target.closest<HTMLButtonElement>("[data-action='load-more']");
      if (button === null) return;
      void loadMore(root, code, button);
    },
    { signal: controller.signal },
  );

  const offs = WATCHED.map((topic) =>
    subscribe(topic, (envelope) => {
      const named =
        envelope.entity.type === "project"
          ? envelope.entity.id
          : readString(envelope.payload, "project_code");
      if (named !== code) return;
      markEvent(envelope.occurred_at);
      void catchUp(root, code);
    }),
  );

  return () => {
    controller.abort();
    for (const off of offs) off();
  };
}

/** Rewrites every server-rendered relative time against the current clock. */
function refreshRelativeTimes(root: HTMLElement): void {
  for (const node of root.querySelectorAll<HTMLElement>("[data-occurred-at]")) {
    const occurredAt = node.dataset.occurredAt;
    if (occurredAt === undefined) continue;
    node.textContent = relativeTime(occurredAt) ?? "";
  }
}

/** Appends the next page. */
async function loadMore(
  root: HTMLElement,
  code: string,
  button: HTMLButtonElement,
): Promise<void> {
  const pageSize = Number(root.dataset.pageSize) || 20;
  const nextPage = (Number(root.dataset.page) || 1) + 1;

  setPending(button, true);
  const result = await getProjectActivity(code, {
    page: nextPage,
    page_size: pageSize,
  });
  setPending(button, false);
  if (!result.ok) return;

  const list = root.querySelector<HTMLElement>("[data-activity-list]");
  if (list === null) return;
  for (const entry of result.data.items) {
    const item = buildEntry(root, entry);
    if (item !== null) list.appendChild(item);
  }

  root.dataset.page = String(nextPage);
  root.dataset.count = String(result.data.count);
  updateFooter(root, button);
}

/**
 * Re-reads the newest page and prepends whatever is newer than the top row.
 *
 * Keyed on the record id, which is monotonic per insert, so a redelivered
 * envelope adds nothing twice.
 */
async function catchUp(root: HTMLElement, code: string): Promise<void> {
  const pageSize = Number(root.dataset.pageSize) || 20;
  const result = await getProjectActivity(code, { page: 1, page_size: pageSize });
  if (!result.ok) return;

  const list = root.querySelector<HTMLElement>("[data-activity-list]");
  if (list === null) return;

  const newest = newestRenderedId(list);
  // The page is newest-first, so walking it backwards keeps insertion order.
  for (const entry of [...result.data.items].reverse()) {
    if (entry.id <= newest) continue;
    const item = buildEntry(root, entry);
    if (item !== null) list.prepend(item);
  }

  root.dataset.count = String(result.data.count);
  const button = root.querySelector<HTMLButtonElement>("[data-action='load-more']");
  updateFooter(root, button);

  const empty = root.querySelector<HTMLElement>("[data-activity-empty]");
  if (empty !== null && list.children.length > 0) empty.hidden = true;
}

/** The highest record id currently on screen, or 0 when the list is empty. */
function newestRenderedId(list: HTMLElement): number {
  let newest = 0;
  for (const node of list.querySelectorAll<HTMLElement>("[data-activity]")) {
    const id = Number(node.dataset.id);
    if (Number.isFinite(id) && id > newest) newest = id;
  }
  return newest;
}

/** Clones one timeline row from the component's template. */
function buildEntry(root: HTMLElement, entry: ActivityEntry): HTMLElement | null {
  const item = cloneTemplate(root, "activity");
  if (item === null) return null;

  item.dataset.id = String(entry.id);
  setField(item, "activity-verb", verbLabel(entry.verb));
  setField(item, "activity-actor", entry.actor);

  const change = activityChange(entry);
  const changeLine = item.querySelector<HTMLElement>("[data-field='activity-change']");
  if (changeLine !== null) {
    changeLine.hidden = change === "";
    changeLine.textContent = change;
  }

  const reasonLine = item.querySelector<HTMLElement>("[data-field='activity-reason']");
  if (reasonLine !== null) {
    reasonLine.hidden = entry.reason === "";
    reasonLine.textContent = entry.reason;
  }

  const when = item.querySelector<HTMLElement>("[data-field='activity-when']");
  if (when !== null) {
    when.dataset.occurredAt = entry.occurred_at;
    when.textContent = relativeTime(entry.occurred_at) ?? "";
    const absolute = formatInstant(entry.occurred_at);
    if (absolute !== null) when.title = absolute;
  }
  return item;
}

/** Keeps the counter and the "Cargar más" control honest about what is left. */
function updateFooter(root: HTMLElement, button: HTMLButtonElement | null): void {
  const list = root.querySelector<HTMLElement>("[data-activity-list]");
  const shown = list?.querySelectorAll("[data-activity]").length ?? 0;
  const total = Number(root.dataset.count) || shown;

  setField(root, "activity-shown", String(shown));
  setField(root, "activity-total", String(total));
  if (button !== null) button.hidden = shown >= total;
}
