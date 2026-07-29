/**
 * Narrowing for the two envelope payloads the Resumen surface patches from.
 *
 * The store hands subscribers `payload: Record<string, unknown>` on purpose: a
 * topic's payload schema is versioned independently and belongs to the
 * subscriber, not to the transport. This module is that subscriber's parser, and
 * it is the only place on this surface where `unknown` is read
 * (`docs/standards/FRONTEND.md` §1).
 *
 * Every parser returns `null` instead of throwing. A payload that does not match
 * the documented shape (`docs/EVENTS.md` §4) means the frame is from a version
 * this build does not understand; dropping the patch leaves the server-rendered
 * value on screen, which is stale but true, whereas a half-applied patch is
 * neither.
 */

import type { ScoreBreakdownEntry } from "../../lib/api/domain";

/** `project.priority.recalculated`, reduced to what a card actually redraws. */
export interface PriorityRecalculated {
  /** The new 0–100 score, exactly as the engine computed it. */
  readonly value: number;
  /** The persisted argument, in the writer's order (contribution descending). */
  readonly breakdown: readonly ScoreBreakdownEntry[];
}

/** `project.state_changed`, reduced to what a state chip needs. */
export interface StateChanged {
  /** The `WorkflowState.code` moved to; the payload never carries its label. */
  readonly to: string;
  /** `BACKLOG | IN_PROGRESS | BLOCKED | DONE | CANCELLED` — safe to branch on. */
  readonly toCategory: string;
}

/**
 * Narrows a `project.priority.recalculated` payload.
 *
 * An entry whose numeric fields are not numbers drops the whole breakdown rather
 * than a single bar: a partial argument beside a full score reads as if the
 * missing signals contributed nothing, which is a stronger claim than "we could
 * not parse this".
 */
export function parsePriorityRecalculated(
  payload: Record<string, unknown>,
): PriorityRecalculated | null {
  const value = payload["value"];
  if (typeof value !== "number" || Number.isNaN(value)) return null;
  const rawBreakdown = payload["breakdown"];
  if (!Array.isArray(rawBreakdown)) return { value, breakdown: [] };

  const breakdown: ScoreBreakdownEntry[] = [];
  for (const entry of rawBreakdown) {
    const parsed = parseSignal(entry);
    if (parsed === null) return { value, breakdown: [] };
    breakdown.push(parsed);
  }
  return { value, breakdown };
}

/** Narrows a `project.state_changed` payload. */
export function parseStateChanged(
  payload: Record<string, unknown>,
): StateChanged | null {
  const to = payload["to"];
  const toCategory = payload["to_category"];
  if (typeof to !== "string" || to === "") return null;
  if (typeof toCategory !== "string" || toCategory === "") return null;
  return { to, toCategory };
}

/** Narrows an `unknown` JSON value to a plain object; arrays and null are not records. */
function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function parseSignal(entry: unknown): ScoreBreakdownEntry | null {
  if (!isRecord(entry)) return null;
  const code = entry["code"];
  const raw = entry["raw"];
  const weight = entry["weight"];
  const contribution = entry["contribution"];
  const reason = entry["reason"];
  if (typeof code !== "string") return null;
  if (typeof raw !== "number" || typeof weight !== "number") return null;
  if (typeof contribution !== "number") return null;
  return {
    code,
    raw,
    weight,
    contribution,
    reason: typeof reason === "string" ? reason : "",
  };
}
