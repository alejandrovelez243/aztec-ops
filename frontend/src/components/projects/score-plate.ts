/**
 * The score plate's behaviour: the ops-lead override, and the live
 * recalculation.
 *
 * Two rules shape it. The override controls are rendered by the server and
 * revealed here, because who is signed in is a browser fact (the session lives
 * in `localStorage`) and a statically rendered page cannot know it; the API is
 * still the answer of record, so a `403` is handled rather than assumed away.
 * And the recalculated envelope carries the whole breakdown
 * (`docs/EVENTS.md` §4), so the plate patches from the payload instead of
 * re-reading the project — the one topic where the payload really is the
 * entity's new value.
 */

import {
  deletePriorityOverride,
  postPriorityOverride,
} from "../../lib/api/client";
import { getOperator } from "../../lib/auth/session";
import { countUp } from "../../lib/motion/spring";
import { subscribe } from "../../lib/stream/store";
import { toast } from "../../lib/toast";
import {
  cloneTemplate,
  isFresher,
  isRecord,
  pulse,
  readNumber,
  setField,
  setPending,
} from "./dom";
import { barWidth, formatContribution, formatScore } from "./format";
import { failureCopy, signalLabel } from "./messages";
import { overrideSummary } from "./presentation";
import type { Override } from "../../lib/api/domain";

/** One breakdown line as the recalculated payload carries it. */
interface SignalPatch {
  readonly code: string;
  readonly contribution: number;
  readonly reason: string;
}

/**
 * Mounts the plate.
 *
 * @param plate - The element carrying `data-score-plate`.
 * @returns The teardown; releases the stream subscription and the listeners.
 */
export function mountScorePlate(plate: HTMLElement): () => void {
  const code = plate.dataset.projectCode ?? "";
  const controller = new AbortController();
  const { signal } = controller;

  revealOpsControls(plate);
  wireOverrideDialog(plate, code, signal);

  const off = subscribe("project.priority.recalculated", (envelope) => {
    if (envelope.entity.id !== code) return;
    if (!isFresher(envelope.occurred_at, plate.dataset.updatedAt)) return;
    const value = readNumber(envelope.payload, "value");
    if (value !== null) patchValue(plate, value);
    patchBreakdown(plate, readBreakdown(envelope.payload["breakdown"]));
    plate.dataset.updatedAt = envelope.occurred_at;
    pulse(plate);
  });

  return () => {
    controller.abort();
    off();
  };
}

/**
 * Shows the override controls to an ops lead.
 *
 * Hidden markup rather than absent markup: the page is server-rendered once for
 * everybody, so the gate has to run in the browser. This is presentation only —
 * the capability itself is enforced by the API, which answers `403` with
 * `details.required = "ops_lead"` to anyone who tries anyway.
 */
function revealOpsControls(plate: HTMLElement): void {
  const ops = plate.querySelector<HTMLElement>("[data-ops-lead]");
  if (ops === null) return;
  ops.hidden = getOperator()?.isOpsLead !== true;
}

/** Wires the dialog: open, close, the amount label, and the two writes. */
function wireOverrideDialog(
  plate: HTMLElement,
  code: string,
  signal: AbortSignal,
): void {
  const dialog = plate.querySelector<HTMLDialogElement>("[data-override-dialog]");
  const form = plate.querySelector<HTMLFormElement>("[data-override-form]");
  const mode = plate.querySelector<HTMLSelectElement>("[data-override-mode]");

  plate.addEventListener(
    "click",
    (event) => {
      const target = event.target;
      if (!(target instanceof Element)) return;

      if (target.closest("[data-action='open-override']") !== null) {
        dialog?.showModal();
        return;
      }
      if (target.closest("[data-action='close-override']") !== null) {
        dialog?.close();
        return;
      }
      const clear = target.closest<HTMLButtonElement>("[data-action='clear-override']");
      if (clear !== null) void revokeOverride(plate, code, clear);
    },
    { signal },
  );

  mode?.addEventListener(
    "change",
    () => {
      setField(plate, "amount-label", mode.value === "boost" ? "Impulso" : "Posición");
    },
    { signal },
  );

  form?.addEventListener(
    "submit",
    (event) => {
      event.preventDefault();
      void saveOverride(plate, code, form, dialog);
    },
    { signal },
  );
}

/**
 * Sends the forced rank.
 *
 * The reason is mandatory in the form for the same reason it is a database
 * check: a forced position with no recorded justification is indistinguishable
 * from a bug three weeks later.
 */
async function saveOverride(
  plate: HTMLElement,
  code: string,
  form: HTMLFormElement,
  dialog: HTMLDialogElement | null,
): Promise<void> {
  const data = new FormData(form);
  const mode = String(data.get("mode") ?? "position");
  const amount = Number(data.get("amount"));
  const reason = String(data.get("reason") ?? "").trim();
  const errorLine = plate.querySelector<HTMLElement>("[data-field='override-error']");

  if (reason === "" || !Number.isFinite(amount)) {
    if (errorLine !== null) {
      errorLine.hidden = false;
      errorLine.textContent = "Indica un valor y el motivo del ajuste.";
    }
    return;
  }
  if (errorLine !== null) errorLine.hidden = true;

  const button = form.querySelector<HTMLButtonElement>("[data-action='submit-override']");
  if (button !== null) setPending(button, true);

  const body =
    mode === "boost"
      ? { boost: amount, reason }
      : { position: Math.trunc(amount), reason };
  const result = await postPriorityOverride(code, body);

  if (button !== null) setPending(button, false);

  if (!result.ok) {
    const copy = failureCopy(result.error);
    toast({ kind: "error", title: copy.title, detail: copy.detail });
    return;
  }

  dialog?.close();
  form.reset();
  applyOverride(plate, result.data.override ?? null);
  const score = result.data.score;
  if (score !== null && score !== undefined) patchValue(plate, score.value);
  toast({
    kind: "success",
    title: "Prioridad ajustada",
    detail: "El puntaje calculado sigue visible junto a tu decisión.",
  });
}

/** Lifts the forced rank; idempotent server-side, so a retry is safe. */
async function revokeOverride(
  plate: HTMLElement,
  code: string,
  button: HTMLButtonElement,
): Promise<void> {
  setPending(button, true);
  const result = await deletePriorityOverride(code);
  setPending(button, false);

  if (!result.ok) {
    const copy = failureCopy(result.error);
    toast({ kind: "error", title: copy.title, detail: copy.detail });
    return;
  }
  applyOverride(plate, null);
  toast({
    kind: "success",
    title: "Ajuste retirado",
    detail: "El proyecto vuelve a ordenarse por su puntaje calculado.",
  });
}

/** Shows or hides the "Ajuste manual" block and its companion control. */
function applyOverride(plate: HTMLElement, forced: Override | null): void {
  const block = plate.querySelector<HTMLElement>("[data-override]");
  const clear = plate.querySelector<HTMLElement>("[data-action='clear-override']");
  if (block !== null) {
    block.hidden = forced === null;
    setField(block, "override-reason", forced?.reason ?? "");
    setField(block, "override-meta", overrideSummary(forced));
  }
  if (clear !== null) clear.hidden = forced === null;
}

/** Counts the headline figure to its new value. */
function patchValue(plate: HTMLElement, value: number): void {
  const figure = plate.querySelector<HTMLElement>("[data-field='score']");
  if (figure === null) return;
  const from = Number.parseFloat(figure.textContent ?? "");
  countUp(figure, Number.isFinite(from) ? from : value, value, formatScore);
}

/**
 * Narrows the payload's `breakdown` into the lines the plate renders.
 *
 * Anything malformed is dropped rather than repaired: an invented contribution
 * would put a number on screen that no policy ever computed.
 */
function readBreakdown(raw: unknown): readonly SignalPatch[] {
  if (!Array.isArray(raw)) return [];
  const items: readonly unknown[] = raw;
  const entries: SignalPatch[] = [];
  for (const item of items) {
    if (!isRecord(item)) continue;
    const code = item["code"];
    const contribution = item["contribution"];
    const reason = item["reason"];
    if (typeof code !== "string" || typeof contribution !== "number") continue;
    entries.push({
      code,
      contribution,
      reason: typeof reason === "string" ? reason : "",
    });
  }
  return entries;
}

/**
 * Rewrites the breakdown from a recalculation.
 *
 * Signals the plate has never rendered are cloned from the component's own
 * `<template>`, and signals the new policy no longer emits are removed — a
 * plate that kept a retired line would be defending the score with an argument
 * the engine no longer makes.
 */
function patchBreakdown(
  plate: HTMLElement,
  entries: readonly SignalPatch[],
): void {
  const list = plate.querySelector<HTMLElement>("[data-breakdown]");
  if (list === null || entries.length === 0) return;

  const total = entries.reduce((sum, entry) => sum + Math.abs(entry.contribution), 0);
  const seen = new Set<string>();

  for (const entry of entries) {
    seen.add(entry.code);
    let row = list.querySelector<HTMLElement>(
      `[data-signal="${CSS.escape(entry.code)}"]`,
    );
    if (row === null) {
      row = cloneTemplate(plate, "signal");
      if (row === null) continue;
      row.dataset.signal = entry.code;
      list.appendChild(row);
    }
    setField(row, "signal-name", signalLabel(entry.code));
    setField(row, "contribution", formatContribution(entry.contribution));
    setField(row, "reason", entry.reason);
    const bar = row.querySelector<HTMLElement>("[data-field='bar']");
    if (bar !== null) {
      bar.style.width = `${barWidth(entry.contribution, total)}%`;
    }
  }

  for (const row of list.querySelectorAll<HTMLElement>("[data-signal]")) {
    const code = row.dataset.signal;
    if (code !== undefined && !seen.has(code)) row.remove();
  }
}
