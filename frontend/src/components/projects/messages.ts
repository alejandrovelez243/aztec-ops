/**
 * Spanish copy for the projects surfaces, derived from the API's *closed*
 * vocabularies and from the typed error envelope.
 *
 * Two rules decide what may live here (`docs/standards/PATTERNS_FRONTEND.md`
 * §7). Operator-editable taxonomies — workflow states, engagement types,
 * clients, priorities — arrive with their own `label` and are never translated
 * here; a map keyed by their codes would drop the value somebody added this
 * morning. Structural vocabularies — activity verbs, blocker kinds, aggregate
 * field names, error codes — change only by migration, and every lookup below
 * falls back to the raw value, so an entry this build has not seen renders
 * itself instead of disappearing.
 *
 * `ApiError.message` is generated English prose for logs (`docs/API.md` §1.5)
 * and is never rendered; the UI branches on `code`/`kind` and speaks its own
 * words.
 */

import type { ApiError } from "../../lib/api/errors";
import type { BlockerKind } from "../../lib/api/domain";

/** A failure as the operator reads it: what happened, and what it means. */
export interface FailureCopy {
  readonly title: string;
  readonly detail: string;
}

/**
 * Aggregate attributes a transition's `requires_fields` can name, and request
 * fields a `validation_error` can blame. Both are attribute names, never
 * labels, so one map serves both.
 */
const FIELD_LABEL: Readonly<Record<string, string>> = {
  name: "nombre",
  summary: "resumen",
  client: "cliente",
  owner: "responsable",
  engagement_type: "tipo de contrato",
  project_type: "tipo de proyecto",
  stage: "etapa",
  start_date: "fecha de inicio",
  target_date: "fecha objetivo",
  business_value: "valor de negocio",
  next_step: "próximo paso",
  to_state: "estado destino",
  reason: "motivo",
  resolution: "resolución",
  description: "descripción",
  body: "texto de la nota",
  position: "posición",
  boost: "impulso",
  kind: "tipo",
  title: "título",
  priority: "prioridad",
  due_date: "fecha límite",
  depends_on: "dependencias",
};

/** Verbs of `ActivityRecord`; the set is closed and versioned by migration. */
const VERB_LABEL: Readonly<Record<string, string>> = {
  CREATED: "Proyecto creado",
  STATE_CHANGED: "Cambio de estado",
  PRIORITY_CHANGED: "Prioridad recalculada",
  BLOCKER_RAISED: "Bloqueo registrado",
  BLOCKER_RESOLVED: "Bloqueo resuelto",
  OWNER_CHANGED: "Cambio de responsable",
  NEXT_STEP_SET: "Próximo paso definido",
  TASK_ADDED: "Tarea agregada",
  NOTE_ADDED: "Nota agregada",
  SEEDED: "Carga inicial",
};

/**
 * The four blocker kinds. Typed as a total record over the generated union, so
 * a kind added by migration fails this file at build time instead of rendering
 * an empty option in the "Registrar bloqueo" form.
 */
const BLOCKER_KIND_LABEL: Readonly<Record<BlockerKind, string>> = {
  EXTERNAL_DEPENDENCY: "Dependencia externa",
  ACCESS: "Acceso o credenciales",
  DECISION: "Decisión pendiente",
  TECHNICAL: "Técnico",
};

/**
 * The same table widened to a string lookup, so an unknown kind arriving on the
 * wire can fall back instead of being cast into the union it is not in.
 */
const BLOCKER_KIND_LOOKUP: Readonly<Record<string, string>> = BLOCKER_KIND_LABEL;

/** Every blocker kind, in the order the form offers them. */
export const BLOCKER_KINDS: readonly BlockerKind[] = [
  "EXTERNAL_DEPENDENCY",
  "ACCESS",
  "DECISION",
  "TECHNICAL",
];

/**
 * Spanish names for the risk flags and priority signals shipped today.
 *
 * Both sets are **open**: a flag or a signal is one class plus one registry
 * entry (CLAUDE.md rule 8), so a code this build has never seen will arrive.
 * That is why every lookup falls back to the code itself and why the flag's own
 * `reason` — the engine's evidence — is always rendered beside the name: an
 * unnamed risk still renders, which is the whole point (a dropped flag is a
 * risk nobody sees). These maps are naming, never behaviour: no branch anywhere
 * asks whether a code is in them.
 */
const RISK_LABEL: Readonly<Record<string, string>> = {
  BLOCKED: "Bloqueado",
  OVERDUE: "Vencido",
  NO_NEXT_STEP: "Sin próximo paso",
  NO_TARGET_DATE: "Sin fecha objetivo",
  STALE: "Sin actividad reciente",
  OWNER_OVERLOADED: "Responsable sobrecargado",
};

const SIGNAL_LABEL: Readonly<Record<string, string>> = {
  deadline_pressure: "Presión de fecha",
  overdue_work: "Trabajo vencido",
  criticality: "Criticidad",
  business_value: "Valor de negocio",
  blockage: "Bloqueo",
  staleness: "Falta de avance",
};

/** Spanish name of an aggregate attribute; unknown names render verbatim. */
export function fieldLabel(field: string): string {
  return FIELD_LABEL[field] ?? field;
}

/** Spanish name of a risk flag; an unknown code renders as itself. */
export function riskLabel(code: string): string {
  return RISK_LABEL[code] ?? code;
}

/** Spanish name of a priority signal; an unknown code renders readably. */
export function signalLabel(code: string): string {
  return SIGNAL_LABEL[code] ?? code.replace(/_/g, " ");
}

/** Spanish name of an activity verb; an unknown verb renders verbatim. */
export function verbLabel(verb: string): string {
  return VERB_LABEL[verb] ?? verb;
}

/** Spanish name of a blocker kind; an unknown kind renders verbatim. */
export function blockerKindLabel(kind: string): string {
  return BLOCKER_KIND_LOOKUP[kind] ?? kind;
}

/** Joins field names into a readable Spanish enumeration. */
export function joinFields(fields: readonly string[]): string {
  const labels = fields.map(fieldLabel);
  if (labels.length <= 1) return labels[0] ?? "";
  const last = labels[labels.length - 1] ?? "";
  return `${labels.slice(0, -1).join(", ")} y ${last}`;
}

/**
 * Maps a typed failure onto the words the operator sees.
 *
 * Each arm names the domain rule that refused, because "algo salió mal" leaves
 * an operator with no next action. `permission_denied` and `conflicting_state`
 * reach the client inside the `unknown` arm — the first is not one of the four
 * documented domain codes and the second has no dedicated arm — so both are
 * recovered from `backendCode` rather than from the prose.
 */
export function failureCopy(error: ApiError): FailureCopy {
  switch (error.kind) {
    case "transition_not_allowed":
      return {
        title: "Ese movimiento ya no es legal",
        detail:
          "El estado del proyecto cambió mientras mirabas esta página. Actualizamos las transiciones disponibles.",
      };
    case "validation": {
      const fields = Object.keys(error.fields);
      return {
        title: "Faltan datos obligatorios",
        detail:
          fields.length > 0
            ? `Revisa: ${joinFields(fields)}.`
            : "El servidor rechazó los datos enviados.",
      };
    }
    case "not_found":
      return {
        title: "No encontramos ese registro",
        detail: "Puede haber sido archivado o renombrado.",
      };
    case "network":
      return {
        title: "Sin conexión con el servidor",
        detail: "Revisa tu red y vuelve a intentarlo.",
      };
    case "unknown":
      return unknownCopy(error.backendCode);
    default:
      return {
        title: "No pudimos completar la acción",
        detail: "Vuelve a intentarlo en unos segundos.",
      };
  }
}

/** Copy for the `unknown` arm, split out so `failureCopy` stays flat. */
function unknownCopy(backendCode: string | null): FailureCopy {
  if (backendCode === "permission_denied") {
    return {
      title: "Necesitas permiso de líder de operaciones",
      detail: "Solo el líder de operaciones puede forzar o levantar la prioridad.",
    };
  }
  if (backendCode === "conflicting_state") {
    return {
      title: "Ese cambio ya estaba aplicado",
      detail: "Alguien más lo hizo primero; recarga para ver el estado actual.",
    };
  }
  if (backendCode === "authentication_required" || backendCode === "invalid_token") {
    return {
      title: "Tu sesión no es válida",
      detail: "Vuelve a iniciar sesión para continuar.",
    };
  }
  return {
    title: "No pudimos completar la acción",
    detail: "Vuelve a intentarlo en unos segundos.",
  };
}

/**
 * Copy for a region that could not be read at all — the `error` arm of the view
 * state, where there is no data to keep on screen.
 */
export function regionFailureCopy(error: ApiError, subject: string): FailureCopy {
  const base = failureCopy(error);
  return {
    title: `No pudimos cargar ${subject}`,
    detail: `${base.title}. ${base.detail}`,
  };
}
