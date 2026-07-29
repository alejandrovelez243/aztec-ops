/**
 * The audit trail's vocabulary, in Spanish, shared by the project timeline and
 * the portfolio-wide feed.
 *
 * Two rules decide what may live here (`docs/standards/PATTERNS_FRONTEND.md`
 * §7). Operator-editable taxonomies — workflow states, engagement types,
 * priorities — arrive with their own `label` and are never translated in the
 * frontend. Structural vocabularies — activity verbs, record origins, entity
 * kinds — change only by migration, and every lookup below falls back to the
 * raw value, so a member this build has not seen renders itself instead of
 * disappearing from a screen whose whole job is to be complete.
 *
 * Signal codes are deliberately absent: `metadata.signal` is rendered verbatim
 * because a table keyed by signal code would silently blank the seventh signal
 * somebody adds with one class and one registry line (CLAUDE.md rule 8).
 */

import type { ActivityEntry } from "../api/domain";

/** One member of a closed vocabulary, as a `<select>` renders it. */
export interface VocabularyOption {
  readonly value: string;
  readonly label: string;
}

/**
 * Verbs of `ActivityRecord`, in the order the filter offers them.
 *
 * The order is editorial — what changed about the work first, what was said
 * about it last — not the database's.
 */
const VERBS: readonly VocabularyOption[] = [
  { value: "CREATED", label: "Proyecto creado" },
  { value: "STATE_CHANGED", label: "Cambio de estado" },
  { value: "PRIORITY_CHANGED", label: "Prioridad recalculada" },
  { value: "OWNER_CHANGED", label: "Cambio de responsable" },
  { value: "NEXT_STEP_SET", label: "Próximo paso definido" },
  { value: "BLOCKER_RAISED", label: "Bloqueo registrado" },
  { value: "BLOCKER_RESOLVED", label: "Bloqueo resuelto" },
  { value: "TASK_ADDED", label: "Tarea agregada" },
  { value: "NOTE_ADDED", label: "Nota agregada" },
  { value: "SEEDED", label: "Carga inicial" },
];

/**
 * Where a record came from: a person, the ranking engine, or the platform.
 *
 * This is the distinction the feed exists to make legible. `MANUAL` carries a
 * mandatory `reason` because a human overruled something; `POLICY` carries the
 * signal and the delta that moved a score; `SYSTEM` is everything the platform
 * wrote on its own.
 */
const ORIGINS: readonly VocabularyOption[] = [
  { value: "MANUAL", label: "Decisión humana" },
  { value: "POLICY", label: "Motor de prioridades" },
  { value: "SYSTEM", label: "Sistema" },
];

/** The three subjects a record can be about. */
const ENTITY_TYPES: readonly VocabularyOption[] = [
  { value: "project", label: "Proyecto" },
  { value: "task", label: "Tarea" },
  { value: "blocker", label: "Bloqueo" },
];

/** Short badge text per origin, for the chip that rides on every row. */
const ORIGIN_BADGE: Readonly<Record<string, string>> = {
  MANUAL: "Manual",
  POLICY: "Motor",
  SYSTEM: "Sistema",
};

/**
 * Tone class per origin.
 *
 * Ámbar for `MANUAL` — a person overruled the engine and that is the row a
 * reader must not skim past; cielo for `POLICY`, the engine's own voice;
 * piedra for `SYSTEM`, which is bookkeeping. An origin this build does not know
 * takes piedra rather than no tone, so it still renders as a chip.
 */
const ORIGIN_TONE: Readonly<Record<string, string>> = {
  MANUAL: "tone-ambar",
  POLICY: "tone-cielo",
  SYSTEM: "tone-piedra",
};

/** Every verb the filter offers, newest-first order irrelevant. */
export function verbOptions(): readonly VocabularyOption[] {
  return VERBS;
}

/** Every origin the filter offers. */
export function originOptions(): readonly VocabularyOption[] {
  return ORIGINS;
}

/** Every entity kind the filter offers. */
export function entityTypeOptions(): readonly VocabularyOption[] {
  return ENTITY_TYPES;
}

/** Spanish name of an activity verb; an unknown verb renders verbatim. */
export function verbLabel(verb: string): string {
  return VERBS.find((option) => option.value === verb)?.label ?? verb;
}

/** Spanish name of an entity kind; an unknown kind renders verbatim. */
export function entityTypeLabel(entityType: string): string {
  return (
    ENTITY_TYPES.find((option) => option.value === entityType)?.label ??
    entityType
  );
}

/** Badge text of an origin; an unknown origin renders verbatim. */
export function originBadge(origin: string): string {
  return ORIGIN_BADGE[origin] ?? origin;
}

/** Tone class of an origin; anything unrecognised is neutral, never untoned. */
export function originTone(origin: string): string {
  return ORIGIN_TONE[origin] ?? "tone-piedra";
}

/**
 * What one record changed, as "antes → después".
 *
 * Both values are stored as strings by the audit trail and either may be empty
 * — a creation has no "before". The empty string is returned when neither side
 * says anything, and the caller hides the line rather than rendering an arrow
 * between two blanks.
 */
export function activityChange(entry: ActivityEntry): string {
  const from = entry.from_value;
  const to = entry.to_value;
  if (from !== "" && to !== "") return `${from} → ${to}`;
  return to !== "" ? to : from;
}

/**
 * The engine's own explanation of a recomputation, from `metadata`.
 *
 * `POLICY` rows carry the signal that moved and by how much
 * (`{"signal": "blockage", "delta": 8.4}`). The signal code is rendered as the
 * engine named it: translating it here would need a table keyed by signal code,
 * which is exactly what makes a new signal invisible instead of merely untranslated.
 *
 * @returns The sentence, or `""` when the record carries no signal — in which
 *   case the caller renders nothing rather than an empty parenthesis.
 */
export function policyExplanation(metadata: ActivityEntry["metadata"]): string {
  if (metadata === undefined) return "";
  const signal = metadata["signal"];
  if (typeof signal !== "string" || signal === "") return "";
  const delta = metadata["delta"];
  if (typeof delta !== "number" || !Number.isFinite(delta)) {
    return `señal ${signal}`;
  }
  const sign = delta < 0 ? "−" : "+";
  return `señal ${signal} · ${sign}${Math.abs(delta).toFixed(1)}`;
}
