/**
 * What a record's lifecycle plate says, and which lifecycles it may be moved onto.
 *
 * Pure: every function here takes the two server answers this surface holds — the record's own
 * read (`workflow`, `state`) and the workflow catalog — and returns view values. Nothing touches
 * the DOM, nothing fetches, so the decision that matters on this screen can be reasoned about and
 * tested without a browser.
 *
 * **The decision that matters.** `PUT /projects/{code}/workflow` refuses when the target graph has
 * no active state carrying the code the record is standing on: a record parked outside its own
 * lifecycle has no column to be drawn in and no move to make, so the server declines to create one
 * (`apps/workflow/services/reassignment.py`). It never asks the caller for a landing state — that
 * would be an unvalidated `workflow_state` write over HTTP, which CLAUDE.md rule 2 forbids — so
 * there is no landing-state question for this UI to ask. What there is instead is a refusal that
 * can be computed *before* the operator commits: the catalog publishes every graph's states, so
 * {@link fitOf} answers the same question the server will, and the option renders unchoosable with
 * the missing state named rather than choosable and refused.
 *
 * The client's answer is never the authority. It hides a choice the server would reject; it does
 * not grant one, and the plate is only ever repainted from the response.
 */

import { failureCopy } from "../projects/messages";
import type { ApiError } from "../../lib/api/errors";
import type {
  WorkflowCatalog,
  WorkflowRef,
  WorkflowShape,
} from "../../lib/api/domain";

/** Which kind of record a plate speaks for. */
export type LifecycleScope = "project" | "task";

/**
 * `Workflow.applies_to` per record kind.
 *
 * Structural vocabulary, not an operator-editable one: a graph governs projects or tasks, the pair
 * changes only by migration, and a graph of the other kind offers moves meant for something else —
 * which the server answers with a 422 (`WorkflowKindMismatch`). Compared as a code, never rendered.
 */
const APPLIES_TO: Readonly<Record<LifecycleScope, string>> = {
  project: "PROJECT",
  task: "TASK",
};

/** Spanish nouns and sentences that differ between a project and a task. */
export interface ScopeCopy {
  /** "el proyecto" / "la tarea", for sentences that name the subject. */
  readonly subject: string;
  /** The same noun opening a sentence; written out rather than capitalised in code. */
  readonly subjectStart: string;
  /** How the inherit entry reads in the picker. */
  readonly inheritName: string;
  /** The plate's sentence when nobody chose this graph for this record. */
  readonly inheritedNote: string;
  /** The plate's sentence when somebody did. */
  readonly directNote: string;
  /** The picker's sub-line for the inherit entry when its target is not knowable. */
  readonly inheritUnknownNote: string;
  /** The empty arm: this deployment has no graph for this kind of record at all. */
  readonly emptyMessage: string;
}

const SCOPE_COPY: Readonly<Record<LifecycleScope, ScopeCopy>> = {
  project: {
    subject: "el proyecto",
    subjectStart: "El proyecto",
    inheritName: "Heredar del tipo de encargo",
    inheritedNote:
      "Nadie eligió este flujo para este proyecto: lo hereda de su tipo de encargo.",
    directNote:
      "Alguien eligió este flujo para este proyecto, por encima de su tipo de encargo.",
    inheritUnknownNote:
      "Lo decide el tipo de encargo del proyecto; el servidor elige el flujo al guardar.",
    emptyMessage: "Todavía no hay ningún flujo para proyectos.",
  },
  task: {
    subject: "la tarea",
    subjectStart: "La tarea",
    inheritName: "Heredar del tipo de encargo del proyecto",
    inheritedNote:
      "Nadie eligió este flujo para esta tarea: lo hereda del tipo de encargo de su proyecto.",
    directNote:
      "Alguien eligió este flujo para esta tarea, por encima del tipo de encargo de su proyecto.",
    inheritUnknownNote:
      "Lo decide el tipo de encargo del proyecto; el servidor elige el flujo al guardar.",
    emptyMessage: "Todavía no hay ningún flujo para tareas.",
  },
};

/** The copy this scope speaks. */
export function scopeCopy(scope: LifecycleScope): ScopeCopy {
  return SCOPE_COPY[scope];
}

/** Narrows a string off the DOM back onto the scope union; anything else is a project. */
export function toScope(value: string | undefined): LifecycleScope {
  return value === "task" ? "task" : "project";
}

/**
 * What an operator can decide here.
 *
 * Two arms rather than a nullable workflow code, because they are two different statements — "this
 * record follows *that* graph" and "this record follows whatever its engagement type binds" — and
 * they travel to two different routes (`PUT` and `DELETE`). A `code: null` would let a caller send
 * "assign nothing", which is not a decision the API accepts on either.
 */
export type LifecycleChoice =
  | { readonly kind: "inherit" }
  | { readonly kind: "workflow"; readonly code: string };

/** The decision currently in force, read off the record's own `workflow`. */
export function currentChoice(workflow: WorkflowRef): LifecycleChoice {
  return workflow.source === "DIRECT"
    ? { kind: "workflow", code: workflow.code }
    : { kind: "inherit" };
}

/** Whether two decisions are the same one; a submit of the current decision is a no-op. */
export function sameChoice(a: LifecycleChoice, b: LifecycleChoice): boolean {
  if (a.kind === "inherit" || b.kind === "inherit") return a.kind === b.kind;
  return a.code === b.code;
}

/** Where `/workflows` draws this graph; the heading it anchors carries the same id. */
export function workflowHref(code: string): string {
  return `/workflows#wf-${code}`;
}

/**
 * Whether a target graph can receive a record standing on `stateCode`.
 *
 * `unknown` is not a hedge, it is the honest arm for the inherit entry when the graph the binding
 * ladder would hand back is not derivable from what this client holds — see
 * {@link inheritedShape}. Claiming "fits" there would promise an outcome nothing checked.
 */
export type LifecycleFit =
  | { readonly kind: "fits" }
  | { readonly kind: "unknown" }
  | { readonly kind: "missing"; readonly offers: readonly string[] };

/**
 * The same question the server asks, against the same rows.
 *
 * A retired state of the target is not a landing site either — `retire_state` refuses to strand
 * records on one — so only `is_active` nodes count, exactly as `validate_reassignment` filters.
 *
 * @param shape - The target graph as `GET /workflows` publishes it.
 * @param stateCode - The state the record is standing on; the code, never its label.
 * @returns `fits` when the target owns an active state with that code, otherwise `missing` with
 *   the labels of the states it does offer — the operator's two ways forward need both.
 */
export function fitOf(shape: WorkflowShape, stateCode: string): LifecycleFit {
  const active = shape.states.filter((state) => state.is_active);
  if (active.some((state) => state.code === stateCode)) return { kind: "fits" };
  return { kind: "missing", offers: active.map((state) => state.label) };
}

/** One row of the picker. */
export interface LifecycleOption {
  /** What choosing this row decides. */
  readonly choice: LifecycleChoice;
  /** Rendered name; the operator's own words for the graph. */
  readonly name: string;
  /** `Workflow.code`, or `""` for the inherit entry, which names no graph. */
  readonly code: string;
  /** The sub-line under the name: why it is offered, or why it cannot be. */
  readonly note: string;
  /** Whether the record could stand in it. `missing` rows are rendered unchoosable. */
  readonly fit: LifecycleFit;
  /** Whether this is the decision already in force. */
  readonly isCurrent: boolean;
}

/** The record a plate speaks for, as its last authoritative read described it. */
export interface LifecycleContext {
  readonly scope: LifecycleScope;
  /** Business code — `PRJ-01`, `PRJ-01-T02`; the address every write is sent to. */
  readonly code: string;
  /** The record's current lifecycle and how it got it. */
  readonly workflow: WorkflowRef;
  /** The state the record stands on — code for the check, label for the sentence. */
  readonly stateCode: string;
  readonly stateLabel: string;
  /**
   * The project's engagement type code, or `null` when it is not knowable here — which is every
   * task, because a task detail carries its project's code and name and not its contract type.
   */
  readonly engagementType: string | null;
}

/**
 * The graph the binding ladder would hand this record back, when that is knowable.
 *
 * Knowable in exactly two situations, and the restraint is the point. When the record already
 * inherits, the graph it follows *is* the ladder's answer. When it does not, the only rung this
 * client can read is the second one — the catalog publishes, per graph, the engagement types whose
 * active binding names it — and a match there beats every rung below it, so the answer is exact.
 *
 * With no match the answer is `null` rather than a guess: rungs three and four (the binding whose
 * engagement type is null, and `is_default`) are indistinguishable from this document, and naming
 * the wrong graph would pre-check a reassignment the server is not going to make.
 */
export function inheritedShape(
  shapes: readonly WorkflowShape[],
  context: LifecycleContext,
): WorkflowShape | null {
  if (context.workflow.source === "INHERITED") {
    return shapes.find((shape) => shape.code === context.workflow.code) ?? null;
  }
  const engagementType = context.engagementType;
  if (engagementType === null) return null;
  return (
    shapes.find((shape) =>
      shape.engagement_types.some((type) => type.code === engagementType),
    ) ?? null
  );
}

/**
 * Every lifecycle this record may be put on, plus the entry that stops choosing for it.
 *
 * Retired graphs are left out: retirement means "stop offering this lifecycle to new work", and
 * moving a record into one is new work by any reading — the server answers `WorkflowRetired`. The
 * graph the record is *already* on stays in the list even when retired, because residents are not
 * arrivals and the row has to be able to say which lifecycle is in force.
 *
 * Order is the catalog's, with the inherit entry first: it is the decision to stop deciding, and it
 * belongs above the graphs it delegates to rather than lost at the end of them.
 *
 * @returns The rows, or an empty list when this deployment publishes no graph for this kind of
 *   record — the picker's `empty` arm, which is a configuration to fix on `/workflows` and not a
 *   failure to retry.
 */
export function buildOptions(
  catalog: WorkflowCatalog,
  context: LifecycleContext,
): readonly LifecycleOption[] {
  const kind = APPLIES_TO[context.scope];
  const shapes = catalog.workflows.filter((shape) => shape.applies_to === kind);
  if (shapes.length === 0) return [];

  const current = currentChoice(context.workflow);
  const offered = shapes.filter(
    (shape) => shape.is_active || shape.code === context.workflow.code,
  );

  return [
    inheritOption(shapes, context, current),
    ...offered.map((shape) => graphOption(shape, context, current)),
  ];
}

/**
 * The compatibility question, asked only of the rows it applies to.
 *
 * A record already following a graph is not arriving in it, so "would this graph accept it" is not
 * a question the row in force has to answer — and answering it could render the lifecycle the
 * record is *on* as unchoosable, which would be the screen claiming the record is somewhere it
 * cannot be. That is reachable: a state retired inside the graph still holds whoever stands on it,
 * while `fitOf` counts only active ones. Residents are never evicted — `WorkflowRetired` refuses
 * arrivals — and the row in force cannot be submitted anyway, because the decision already in force
 * is not a change.
 */
function fitWhenOffered(
  shape: WorkflowShape,
  stateCode: string,
  isCurrent: boolean,
): LifecycleFit {
  return isCurrent ? { kind: "fits" } : fitOf(shape, stateCode);
}

/** The row that hands the decision back to the engagement type. */
function inheritOption(
  shapes: readonly WorkflowShape[],
  context: LifecycleContext,
  current: LifecycleChoice,
): LifecycleOption {
  const copy = SCOPE_COPY[context.scope];
  const isCurrent = sameChoice(current, { kind: "inherit" });
  const target = inheritedShape(shapes, context);
  const fit =
    target === null
      ? { kind: "unknown" as const }
      : fitWhenOffered(target, context.stateCode, isCurrent);
  return {
    choice: { kind: "inherit" },
    name: copy.inheritName,
    code: "",
    note:
      target === null
        ? copy.inheritUnknownNote
        : noteFor(
            fit,
            target.name,
            context,
            `Hoy le corresponde «${target.name}».`,
          ),
    fit,
    isCurrent,
  };
}

/** The row that puts the record on one named graph. */
function graphOption(
  shape: WorkflowShape,
  context: LifecycleContext,
  current: LifecycleChoice,
): LifecycleOption {
  const choice: LifecycleChoice = { kind: "workflow", code: shape.code };
  const isCurrent = sameChoice(current, choice);
  const fit = fitWhenOffered(shape, context.stateCode, isCurrent);
  const settled = isCurrent
    ? "Es el flujo elegido para este registro."
    : `Sus estados y sus movimientos pasarían a mandar sobre ${SCOPE_COPY[context.scope].subject}.`;
  return {
    choice,
    name: shape.name,
    code: shape.code,
    note: noteFor(fit, shape.name, context, settled),
    fit,
    isCurrent,
  };
}

/**
 * The sub-line of a row: the refusal when there is one, otherwise what choosing it would mean.
 *
 * The refusal names the state that has no home *and* what the target does offer, because those are
 * the operator's two ways forward — move the record first, or add the missing column to that graph
 * — and each needs a different one of those facts (`config/errors.py`, `_incompatible_state_details`).
 */
function noteFor(
  fit: LifecycleFit,
  name: string,
  context: LifecycleContext,
  settled: string,
): string {
  if (fit.kind !== "missing") return settled;
  const offers =
    fit.offers.length === 0
      ? "Ese flujo todavía no tiene estados."
      : `Ofrece: ${fit.offers.join(", ")}.`;
  return `«${name}» no tiene «${context.stateLabel}», donde está ahora ${SCOPE_COPY[context.scope].subject}. ${offers}`;
}

/** A refusal as the operator reads it, rendered where they are looking. */
export interface RefusalCopy {
  readonly title: string;
  readonly detail: string;
}

/** Narrows an `unknown` JSON value to a plain object; arrays and `null` are not records. */
function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/** The envelope's `details`, or `{}` for the arms that carry none (a dead network). */
function detailsOf(error: ApiError): Record<string, unknown> {
  return "details" in error && isRecord(error.details) ? error.details : {};
}

/** Keeps the string entries of a list; anything else means the wire lied about its shape. */
function readStrings(value: unknown): readonly string[] {
  if (!Array.isArray(value)) return [];
  return value.filter((entry): entry is string => typeof entry === "string");
}

/**
 * What the server said when it refused, in this screen's own words.
 *
 * Only two refusals are dressed here and both are `409 conflicting_state`, told apart by
 * `details.current` exactly as the backend writes it. Everything else — a lost network, a session
 * that expired, a permission the browser thought it had — falls through to the shared
 * {@link failureCopy}, because none of those are about lifecycles and inventing a lifecycle
 * sentence for them would send the operator looking in the wrong place.
 *
 * The state codes in `details.available` are translated through the catalog the picker already
 * holds, so what is rendered is the operator's own labels (CLAUDE.md rule 1); a code the catalog
 * does not carry renders as itself rather than disappearing.
 */
export function refusalCopy(
  error: ApiError,
  context: LifecycleContext,
  shapes: readonly WorkflowShape[],
): RefusalCopy {
  const details = detailsOf(error);
  if (error.code !== "conflicting_state") {
    const copy = failureCopy(error);
    return { title: copy.title, detail: copy.detail };
  }

  const targetCode =
    typeof details["workflow"] === "string" ? details["workflow"] : "";
  const target = shapes.find((shape) => shape.code === targetCode) ?? null;
  const targetName = target?.name ?? targetCode;

  if (details["current"] === "retired") {
    return {
      title: `«${targetName}» está retirado`,
      detail:
        "Un flujo retirado sigue mandando sobre lo que ya está dentro, pero no admite registros nuevos. Vuelve a activarlo en Flujos de trabajo o elige otro.",
    };
  }

  const stateCode =
    typeof details["id"] === "string" ? details["id"] : context.stateCode;
  const stateLabel =
    stateCode === context.stateCode ? context.stateLabel : stateCode;
  const offers = labelsOf(target, readStrings(details["available"]));
  return {
    title: `«${targetName}» no tiene «${stateLabel}»`,
    detail:
      `${SCOPE_COPY[context.scope].subjectStart} está en «${stateLabel}» y ese flujo no tiene ese estado. ` +
      (offers.length === 0
        ? "Añade ese estado a ese flujo, o mueve el registro antes de cambiarlo."
        : `Muévelo a uno de los suyos — ${offers.join(", ")} — o añade «${stateLabel}» a ese flujo.`),
  };
}

/** State codes as the target graph labels them; an unknown code renders as itself. */
function labelsOf(
  shape: WorkflowShape | null,
  codes: readonly string[],
): readonly string[] {
  if (shape === null) return codes;
  return codes.map(
    (code) => shape.states.find((state) => state.code === code)?.label ?? code,
  );
}
