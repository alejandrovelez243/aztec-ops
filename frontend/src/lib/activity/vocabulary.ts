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

/**
 * The actor the platform signs its own records with.
 *
 * It is not a person and must never be offered as one: the engine writes
 * records too, and a fake avatar for it would be the interface claiming a
 * colleague did something nobody did.
 */
export const SYSTEM_ACTOR = "system";

/** How the platform's signature reads on screen. */
export const SYSTEM_ACTOR_LABEL = "Sistema";

/** One member of a closed vocabulary, as a dropdown renders it. */
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
  // Kind-neutral on purpose: `CREATED` is written for a project, a task and a
  // person alike, and the subject column already says which. "Proyecto creado"
  // was a lie on the row about somebody joining the team.
  { value: "CREATED", label: "Alta" },
  { value: "STATE_CHANGED", label: "Cambio de estado" },
  // Beside the state change and not among the roster's facts: it is the same
  // subject — how this record is allowed to move — and the trail is where "why
  // do its buttons differ from yesterday's" is answered.
  { value: "WORKFLOW_ASSIGNED", label: "Cambio de flujo" },
  { value: "PRIORITY_CHANGED", label: "Prioridad recalculada" },
  { value: "OWNER_CHANGED", label: "Cambio de responsable" },
  { value: "NEXT_STEP_SET", label: "Próximo paso definido" },
  { value: "BLOCKER_RAISED", label: "Bloqueo registrado" },
  { value: "BLOCKER_RESOLVED", label: "Bloqueo resuelto" },
  { value: "TASK_ADDED", label: "Tarea agregada" },
  { value: "NOTE_ADDED", label: "Nota agregada" },
  // The roster's own facts. Capacity is the one that changes what "sobrecarga"
  // means, so a flag nobody can explain is a flag nobody trusts
  // (`apps/activity/models.py`, `EntityType.MEMBER`).
  { value: "RENAMED", label: "Nombre cambiado" },
  { value: "ROLE_CHANGED", label: "Rol cambiado" },
  { value: "CAPACITY_CHANGED", label: "Capacidad cambiada" },
  { value: "DEACTIVATED", label: "Persona retirada" },
  { value: "REACTIVATED", label: "Persona restaurada" },
  { value: "PASSWORD_RESET", label: "Contraseña restablecida" },
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

/**
 * What a record can be about (`apps/activity/models.py`, `EntityType`).
 *
 * A person and a taxonomy row are subjects with the same standing as a project:
 * capacity is what "overloaded" is measured against, and renaming a role changes
 * what half the product means.
 */
const ENTITY_TYPES: readonly VocabularyOption[] = [
  { value: "project", label: "Proyecto" },
  { value: "task", label: "Tarea" },
  { value: "blocker", label: "Bloqueo" },
  { value: "member", label: "Persona" },
  { value: "role", label: "Rol" },
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

/**
 * Turns a wire code into something a person can read: `PASSWORD_RESET` →
 * `Password reset`.
 *
 * The fallback for every vocabulary below. A verb the backend adds tomorrow will
 * reach this build before its Spanish name does, and `DEACTIVATED` shouted in a
 * table cell is a defect shipped to the operator; a sentence-cased phrase is
 * merely untranslated. It is deliberately not disguised as Spanish — reading
 * English in one cell is the visible reminder that the entry above is owed.
 */
function humanizeCode(code: string): string {
  const words = code.trim().toLowerCase().replace(/_+/g, " ");
  if (words === "") return code;
  return words.charAt(0).toUpperCase() + words.slice(1);
}

/** Spanish name of an activity verb; an unknown verb degrades to a phrase. */
export function verbLabel(verb: string): string {
  return (
    VERBS.find((option) => option.value === verb)?.label ?? humanizeCode(verb)
  );
}

/** Spanish name of an entity kind; an unknown kind degrades to a phrase. */
export function entityTypeLabel(entityType: string): string {
  return (
    ENTITY_TYPES.find((option) => option.value === entityType)?.label ??
    humanizeCode(entityType)
  );
}

/** Badge text of an origin; an unknown origin degrades to a phrase. */
export function originBadge(origin: string): string {
  return ORIGIN_BADGE[origin] ?? humanizeCode(origin);
}

/** Tone class of an origin; anything unrecognised is neutral, never untoned. */
export function originTone(origin: string): string {
  return ORIGIN_TONE[origin] ?? "tone-piedra";
}

/** The two values a boolean field is stored as by the trail. */
const BOOLEAN_VALUES: readonly string[] = ["true", "false"];

/**
 * Whether both sides of a record are the flag some verb already names.
 *
 * `DEACTIVATED` records `is_active: true → false`. Rendering that is worse than
 * rendering nothing: it is not Spanish, it does not say which flag moved, and
 * the verb beside it ("Persona retirada") has already said exactly what
 * happened. So the change cell stays empty and the verb carries the fact.
 */
function isBooleanChange(from: string, to: string): boolean {
  return BOOLEAN_VALUES.includes(from) && BOOLEAN_VALUES.includes(to);
}

/**
 * What one record changed, as "antes → después".
 *
 * Both values are stored as strings by the audit trail and either may be empty
 * — a creation has no "before". The empty string is returned when neither side
 * says anything, and the caller hides the line rather than rendering an arrow
 * between two blanks.
 *
 * A `true → false` pair is treated as saying nothing, for the reason in
 * {@link isBooleanChange}.
 */
export function activityChange(entry: ActivityEntry): string {
  const from = entry.from_value;
  const to = entry.to_value;
  if (isBooleanChange(from, to)) return "";
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
