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

import { verbLabel } from "../../lib/activity/vocabulary";
import type { ApiError } from "../../lib/api/errors";
import type { BlockerKind } from "../../lib/api/domain";

/**
 * Activity verbs are named by the shared trail vocabulary
 * (`lib/activity/vocabulary.ts`), which the portfolio-wide feed renders from
 * too — re-exported here so the timeline's call sites keep one import and the
 * two surfaces cannot name the same verb differently.
 */
export { verbLabel };

/** A failure as the operator reads it: what happened, and what it means. */
export interface FailureCopy {
  readonly title: string;
  readonly detail: string;
}

/**
 * Why a task row's move control is dead, when it is.
 *
 * The list read (`TaskView`) does not publish `transitions`, and the legal moves
 * out of a state are the only source a transition control may have
 * (`docs/API.md` §2.2) — deriving them from the state code is the guess this
 * codebase refuses. So the control renders visibly dead until the field lands,
 * which `task-moves.ts` picks up with no further change.
 *
 * The sentence stays in the operator's terms: they do not need to know which
 * payload is short a field, only that this is not their mistake and that no
 * choice of theirs is being ignored.
 */
export const TASK_MOVES_UNAVAILABLE =
  "Mover la tarea todavía no está disponible desde esta pantalla: aún no sabemos qué movimientos permite su flujo.";

/** Why the create-task form refuses to submit. */
export const CREATE_INCOMPLETE =
  "Escribe un título y elige una prioridad para crear la tarea.";

/**
 * Why the create-task dialog cannot submit at all.
 *
 * The API requires a priority, so an empty catalog leaves nothing legal to send.
 * The sentence names both the cause and the way out: a dead button with no
 * explanation reads as a broken screen, and the operator keeps pressing it.
 */
export const CREATE_NO_PRIORITIES =
  "No pudimos leer el catálogo de prioridades, así que ahora no se pueden crear tareas. Vuelve a cargar la página.";

/**
 * Why the priority chip cannot be used as a picker.
 *
 * The options are the catalog's, so a failed catalog read leaves nothing legal
 * to offer. The chip still reports the priority the task has — that fact came
 * from the task read and is unaffected — and goes visibly dead with this
 * sentence rather than disappearing: a control that vanishes teaches the
 * operator that the capability does not exist (FRONTEND.md §8).
 */
export const PRIORITY_UNAVAILABLE =
  "No pudimos leer el catálogo de prioridades, así que ahora no se puede cambiar. Vuelve a cargar la página.";

/**
 * Why the task header shows its prerequisites without letting them be edited.
 *
 * The only thing a task may depend on is another task of its own project, so the
 * picker's options are a second read — and that read is not allowed to take the
 * task screen down. When it fails the chips stay on screen, because they are part
 * of the task and came from the task's own read, and this sentence says why they
 * cannot be touched (FRONTEND.md §8): a control that vanishes teaches the
 * operator that the capability does not exist.
 */
export const DEPENDENCIES_UNAVAILABLE =
  "No pudimos leer las demás tareas del proyecto, así que ahora no se pueden cambiar las dependencias. Vuelve a cargar la página.";

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
  engagement_type: "tipo de encargo",
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
const BLOCKER_KIND_LOOKUP: Readonly<Record<string, string>> =
  BLOCKER_KIND_LABEL;

/** Every blocker kind, in the order the form offers them. */
export const BLOCKER_KINDS: readonly BlockerKind[] = [
  "EXTERNAL_DEPENDENCY",
  "ACCESS",
  "DECISION",
  "TECHNICAL",
];

/** Spanish name of an aggregate attribute; unknown names render verbatim. */
export function fieldLabel(field: string): string {
  return FIELD_LABEL[field] ?? field;
}

/**
 * The label the API shipped for a risk flag, with its code as the last resort.
 *
 * The Spanish wording belongs to the specification that raised the flag, not
 * to this file: a criterion is one class plus one registry entry (CLAUDE.md
 * rule 8), and a table here would have meant a seventh criterion rendering as
 * `OWNER_OVERLOADED` until somebody remembered to translate it.
 */
export function riskLabel(flag: { label?: string; code: string }): string {
  const label = flag.label ?? "";
  return label === "" ? flag.code : label;
}

/**
 * The label the API persisted with a signal's contribution, code as fallback.
 *
 * The breakdown is an audit artifact: it carries the wording that was true when
 * the score was computed, which is why the label travels with it instead of
 * being resolved from a table at render time.
 */
export function signalLabel(entry: { label?: string; code: string }): string {
  const label = entry.label ?? "";
  return label === "" ? entry.code : label;
}

/** Spanish name of a blocker kind; an unknown kind renders verbatim. */
export function blockerKindLabel(kind: string): string {
  return BLOCKER_KIND_LOOKUP[kind] ?? kind;
}

/** The request field a rejected prerequisite is blamed on, on both sides of the wire. */
const DEPENDENCY_FIELD = "depends_on";

/**
 * The task codes of a refused dependency loop, in the order they close it.
 *
 * `[]` for every other `conflicting_state`, which is what keeps the generic
 * sentence reachable: a caller cannot tell the two apart from the code alone.
 * Non-string entries are dropped rather than rendered, because the chain is
 * shown to the operator as the links to break.
 */
function readCycle(details: Record<string, unknown>): readonly string[] {
  const cycle = details["cycle"];
  if (!Array.isArray(cycle)) return [];
  return cycle.filter((entry): entry is string => typeof entry === "string");
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
      // A rejected prerequisite is not a missing field, and telling the operator
      // to fill something in would send them looking for a blank that is not
      // there: the server refuses `depends_on` when it names a task of another
      // project, which is a rule about the graph, not about completeness.
      if (fields.length === 1 && fields[0] === DEPENDENCY_FIELD) {
        return {
          title: "Esa dependencia no es válida",
          detail:
            "Una tarea solo puede depender de otras tareas de su mismo proyecto.",
        };
      }
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
      return unknownCopy(error.backendCode, error.details);
    case "auth":
    case "permission_denied":
    default:
      return {
        title: "No pudimos completar la acción",
        detail: "Vuelve a intentarlo en unos segundos.",
      };
  }
}

/** Copy for the `unknown` arm, split out so `failureCopy` stays flat. */
function unknownCopy(
  backendCode: string | null,
  details: Record<string, unknown>,
): FailureCopy {
  // `docs/API.md` §1.2.4 documents `permission_denied` with
  // `details.required = "ops_lead"`; `ops_lead_required` is accepted beside it
  // so a rename on the server surfaces as the right sentence rather than as
  // "algo salió mal".
  if (
    backendCode === "permission_denied" ||
    backendCode === "ops_lead_required"
  ) {
    return {
      title: "Necesitas permiso de líder de operaciones",
      detail:
        "Solo el líder de operaciones puede forzar o levantar la prioridad.",
    };
  }
  if (backendCode === "conflicting_state") {
    // One 409 carries a second meaning, and it is the opposite of "somebody got
    // there first": a `depends_on` that would close a loop is refused with the
    // chain that would have closed (`config/errors.py`, `_cycle_details`).
    // Naming the chain is the whole point — it is the only thing that tells the
    // operator which link to break.
    const cycle = readCycle(details);
    if (cycle.length > 0) {
      return {
        title: "Esa dependencia haría un círculo",
        detail: `${cycle.join(" → ")} acabaría esperándose a sí misma. Quita uno de esos enlaces y vuelve a intentarlo.`,
      };
    }
    return {
      title: "Ese cambio ya estaba aplicado",
      detail: "Alguien más lo hizo primero; recarga para ver el estado actual.",
    };
  }
  if (
    backendCode === "authentication_required" ||
    backendCode === "invalid_token"
  ) {
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
export function regionFailureCopy(
  error: ApiError,
  subject: string,
): FailureCopy {
  const base = failureCopy(error);
  return {
    title: `No pudimos cargar ${subject}`,
    detail: `${base.title}. ${base.detail}`,
  };
}
