/**
 * The board's move use case: which transitions an aggregate may take, and how one is submitted.
 *
 * Four input paths — dragging a project on the rail, dragging a task on the board, and the
 * "Mover a…" menu on either card — go through this module, so legality is asked for in one way
 * and refusals are worded in one way. Splitting them is how a drag ends up allowed where the
 * menu disables it.
 *
 * Three invariants:
 *
 * - **The API owns legality.** A project's legal moves are `project.transitions` and nothing
 *   else (`docs/API.md` §2.2); this module never derives a move from a state code, so a
 *   workflow edge added in the admin appears on the board with no frontend change.
 * - **A task's legality is the server's to refuse.** `GET /api/v1/tasks/{code}` publishes
 *   `transitions`, but `src/lib/api/client.ts` exposes no reader for it and this task may not
 *   edit `lib/`. So a task drag offers every published state of its workflow, posts, and lets
 *   `POST /api/v1/tasks/{code}/transition` refuse — the refusal path (travel back, shake, a
 *   toast naming the rule) is the same one a stale project button already takes. The typed 409
 *   carries `allowed`, so the *first* refusal teaches this board the task's real legal set and
 *   every later gesture on that card is painted from it.
 * - **Nothing is painted optimistically.** {@link submitMove} and {@link submitTaskMove}
 *   return the server's answer; the caller keeps the card pending until the matching envelope
 *   arrives (`docs/standards/PATTERNS_FRONTEND.md` §8).
 */
import { getProject, postProjectTransition, postTaskTransition } from "../../lib/api/client";
import type { ApiError } from "../../lib/api/errors";
import type { ProjectDetail, TaskItem } from "../../lib/api/domain";
import type { Result } from "../../lib/api/client";
import { fieldList } from "./board-model";

/** One legal move out of the aggregate's current state, ready to render as a target. */
export interface MoveOption {
  readonly toStateCode: string;
  readonly label: string;
  readonly requiresReason: boolean;
  /**
   * Attributes the transition requires that are currently empty on the aggregate.
   *
   * Non-empty means the move is legal in the workflow but refused today: the control renders
   * present and visibly dead, naming the field, instead of letting the operator discover it
   * through a 422 (`docs/standards/FRONTEND.md` §8).
   */
  readonly missingFields: readonly string[];
}

/** What the board learned when it asked an aggregate for its legal moves. */
export type MovesResult =
  | {
      readonly kind: "ready";
      readonly options: readonly MoveOption[];
      readonly stateLabel: string;
      /**
       * True when the list is the workflow's whole state set rather than a checked legal set —
       * the task path before its first refusal. The menu says so out loud, because offering a
       * move the server may refuse without warning would be the interface lying by omission.
       */
      readonly isProvisional: boolean;
    }
  | { readonly kind: "error"; readonly title: string; readonly detail: string };

/** A failure worded for a person: what was refused, and which rule refused it. */
export interface MoveErrorCopy {
  readonly title: string;
  readonly detail: string;
}

/** One state a card could be moved into, as the DOM already renders it. */
export interface StateOption {
  readonly code: string;
  readonly label: string;
}

/**
 * What a refusal is about, so every sentence names the right thing.
 *
 * `noun` is the lowercase Spanish noun with its article ("el proyecto", "la tarea"); the copy
 * below is written so that it and the code are the only variable parts, which is what keeps
 * one set of sentences serving both surfaces.
 */
export interface MoveSubject {
  readonly code: string;
  readonly noun: string;
}

/** Names one project in a refusal. */
export function projectSubject(code: string): MoveSubject {
  return { code, noun: "el proyecto" };
}

/** Names one task in a refusal. */
export function taskSubject(code: string): MoveSubject {
  return { code, noun: "la tarea" };
}

/** Resolved legal moves per project code; a drag re-grabs the same card constantly. */
const cache = new Map<string, MovesResult>();

/** In-flight lookups, so a fast second grab joins the first request instead of racing it. */
const inFlight = new Map<string, Promise<MovesResult>>();

/** Legal target states per task code, learned from a refusal the server already worded. */
const taskAllowed = new Map<string, ReadonlySet<string>>();

/** Task edges the server has told us demand a reason, keyed `from>to`. */
const taskReasonEdges = new Set<string>();

/**
 * The legal moves of one project, cached for the life of the page.
 *
 * The cache is what makes the drag feel instant on the second grab; it is invalidated by
 * {@link invalidateMoves} whenever the project's state changes, because the legal set is a
 * function of where the project currently sits.
 *
 * Never throws: a failed lookup resolves to the `error` arm with copy the caller can show.
 */
export async function loadMoves(code: string): Promise<MovesResult> {
  const cached = cache.get(code);
  if (cached !== undefined) return cached;
  const running = inFlight.get(code);
  if (running !== undefined) return running;

  const request = getProject(code)
    .then((result) => {
      const resolved = toMovesResult(result);
      if (resolved.kind === "ready") cache.set(code, resolved);
      return resolved;
    })
    .finally(() => {
      inFlight.delete(code);
    });
  inFlight.set(code, request);
  return request;
}

/** Forgets one project's legal moves; call it whenever its state changed. */
export function invalidateMoves(code: string): void {
  cache.delete(code);
}

/**
 * The moves one task may be offered, from the states its own workflow publishes.
 *
 * Before the first refusal every other state is offered and the result is marked provisional:
 * the board cannot know the edges, and refusing to offer any move would make the surface
 * useless for the sake of a purity the server already enforces. After a refusal the task's
 * `allowed` list — the one the typed 409 carried — is the whole answer, and the result stops
 * being provisional.
 *
 * Pure and synchronous: no request is made, so a grab paints legality on the same frame.
 */
export function taskMoves(
  code: string,
  states: readonly StateOption[],
  currentStateCode: string,
  currentStateLabel: string,
): MovesResult {
  const learned = taskAllowed.get(code);
  const targets = states.filter((state) => {
    if (state.code === currentStateCode) return false;
    return learned === undefined || learned.has(state.code);
  });
  return {
    kind: "ready",
    stateLabel: currentStateLabel,
    isProvisional: learned === undefined,
    options: targets.map((state) => ({
      toStateCode: state.code,
      label: state.label,
      requiresReason: taskReasonEdges.has(edgeKey(currentStateCode, state.code)),
      missingFields: [],
    })),
  };
}

/**
 * Records what the server said one task may actually do, from a refused transition's
 * `allowed`. The next grab of that card paints only those columns as legal.
 */
export function rememberTaskMoves(
  code: string,
  allowed: readonly string[],
): void {
  taskAllowed.set(code, new Set(allowed));
}

/** Records that one task edge demands a reason, so the next attempt asks before posting. */
export function rememberTaskReason(
  fromStateCode: string,
  toStateCode: string,
): void {
  taskReasonEdges.add(edgeKey(fromStateCode, toStateCode));
}

/** Forgets a task's learned legal set; call it whenever its state changed. */
export function invalidateTaskMoves(code: string): void {
  taskAllowed.delete(code);
}

function edgeKey(from: string, to: string): string {
  return `${from}>${to}`;
}

/**
 * Executes one project transition (`POST /api/v1/projects/{code}/transition`).
 *
 * The returned project is the server's word on the move, but it is not the cue to repaint:
 * the caller holds it until the matching envelope arrives, or until the confirmation deadline
 * passes without one.
 */
export async function submitMove(
  code: string,
  toStateCode: string,
  reason: string,
): Promise<Result<ProjectDetail>> {
  invalidateMoves(code);
  return postProjectTransition(code, { to_state: toStateCode, reason });
}

/**
 * Executes one task transition (`POST /api/v1/tasks/{code}/transition`).
 *
 * Same contract as {@link submitMove}: the response proves the write, the envelope repaints.
 */
export async function submitTaskMove(
  code: string,
  toStateCode: string,
  reason: string,
): Promise<Result<TaskItem>> {
  return postTaskTransition(code, { to_state: toStateCode, reason });
}

/**
 * Words a refusal in Spanish, naming the domain rule that produced it.
 *
 * `error.message` is generated English prose for logs and is never shown (`docs/API.md`
 * §4.2); every branch here keys on `code` / `backendCode`, so a message reworded server-side
 * cannot change what the operator reads.
 */
export function moveErrorCopy(
  error: ApiError,
  subject: MoveSubject,
  targetLabel: string,
): MoveErrorCopy {
  switch (error.kind) {
    case "transition_not_allowed":
      return {
        title: `${subject.code} no puede pasar a ${targetLabel}`,
        detail:
          "El flujo de trabajo no tiene esa transición desde el estado actual. El tablero tenía una lista vieja de movimientos; ya la descartó.",
      };
    case "validation":
      return {
        title: `${subject.code} sigue en su estado`,
        detail: validationDetail(error.fields, subject.noun),
      };
    case "not_found":
      return {
        title: `${subject.code} ya no existe`,
        detail: `Actualiza el tablero para ver ${subject.noun} tal como está en el servidor.`,
      };
    case "auth":
      return {
        title: "Tu sesión ya no es válida",
        detail: `${subject.code} sigue donde estaba. Vuelve a iniciar sesión para mover trabajo.`,
      };
    case "permission_denied":
      return {
        title: "No tienes permiso para este movimiento",
        detail: `Requiere la capacidad ${capabilityName(error.required)}. Pídesela a quien administra el portafolio.`,
      };
    case "network":
      return {
        title: "Sin conexión con el servidor",
        detail: `El movimiento de ${subject.code} no se guardó. Vuelve a intentarlo.`,
      };
    case "unknown":
      return unknownCopy(error.backendCode, subject);
    default:
      return exhausted(error);
  }
}

/**
 * Spanish name of a capability the API named in a 403.
 *
 * `ops_lead` is the only one the product has today; anything else renders under its wire
 * name, because inventing a translation for a capability we do not know is how a refusal
 * stops being actionable.
 */
function capabilityName(required: string): string {
  return required === "ops_lead" ? "«líder de operaciones»" : `«${required}»`;
}

/**
 * Words a failed *lookup* — asking a project what it may do, not asking it to do something.
 *
 * Kept apart from {@link moveErrorCopy} because the two failures are different sentences: one
 * says a move was refused, the other says we never learned which moves exist.
 */
function lookupErrorCopy(error: ApiError): MoveErrorCopy {
  switch (error.kind) {
    case "network":
      return {
        title: "Sin conexión con el servidor",
        detail:
          "No pudimos consultar los movimientos legales de este proyecto. Vuelve a intentarlo.",
      };
    case "not_found":
      return {
        title: "El proyecto ya no está en el portafolio",
        detail: "Actualiza el tablero para ver el estado real.",
      };
    case "auth":
      return {
        title: "Tu sesión ya no es válida",
        detail: "Vuelve a iniciar sesión para consultar y mover trabajo.",
      };
    case "transition_not_allowed":
    case "validation":
    case "permission_denied":
    case "unknown":
    default:
      return {
        title: "No pudimos consultar los movimientos",
        detail:
          "El servidor no devolvió las transiciones legales, así que el tablero no puede ofrecer ninguna.",
      };
  }
}

/**
 * The `error` arm of a moves lookup, plus the legality of every transition the project
 * reported: legal in the workflow, and satisfiable with the data the project has today.
 */
function toMovesResult(result: Result<ProjectDetail>): MovesResult {
  if (!result.ok) {
    const copy = lookupErrorCopy(result.error);
    return { kind: "error", title: copy.title, detail: copy.detail };
  }
  const project = result.data;
  const empty = emptyAttributes(project);
  return {
    kind: "ready",
    stateLabel: project.state.label,
    isProvisional: false,
    options: project.transitions.map((transition) => ({
      toStateCode: transition.to_state.code,
      label: transition.label,
      requiresReason: transition.requires_reason,
      missingFields: transition.requires_fields.filter((field) =>
        empty.has(field),
      ),
    })),
  };
}

/**
 * The project attributes `requires_fields` can name, and whether each is currently empty.
 *
 * An attribute the frontend does not know about counts as empty: the control then renders
 * dead and names the field, which is the honest answer to a contract that grew server-side —
 * the opposite mistake enables a move the API is about to refuse.
 */
function emptyAttributes(project: ProjectDetail): ReadonlySet<string> {
  const values: Record<string, unknown> = {
    name: project.name,
    summary: project.summary ?? null,
    next_step: project.next_step ?? null,
    target_date: project.target_date ?? null,
    start_date: project.start_date ?? null,
    business_value: project.business_value ?? null,
    owner: project.owner ?? null,
    stage: project.stage ?? null,
    project_type: project.project_type ?? null,
  };
  const empty = new Set<string>();
  for (const transition of project.transitions) {
    for (const field of transition.requires_fields) {
      const value = values[field];
      if (value === undefined || value === null || value === "") {
        empty.add(field);
      }
    }
  }
  return empty;
}

function validationDetail(
  fields: Record<string, string[]>,
  noun: string,
): string {
  const names = Object.keys(fields);
  if (names.length === 0) {
    return "El servidor rechazó los datos del movimiento.";
  }
  if (names.includes("reason")) {
    return "Esta transición exige un motivo. Vuelve a intentarlo y el tablero te lo pedirá.";
  }
  return `Faltan datos en ${noun}: ${fieldList(names)}.`;
}

function unknownCopy(
  backendCode: string | null,
  subject: MoveSubject,
): MoveErrorCopy {
  if (backendCode === "conflicting_state") {
    return {
      title: `${subject.code} cambió mientras lo movías`,
      detail: `Alguien más tocó ${subject.noun}. El tablero ya muestra el estado real.`,
    };
  }
  return {
    title: `No pudimos mover ${subject.code}`,
    detail:
      "El servidor respondió con un error inesperado. Vuelve a intentarlo.",
  };
}

/**
 * Unreachable while `ApiError` is fully handled above; reaching it means a new arm was added
 * to the union and this switch was not updated, which the compiler flags first.
 */
function exhausted(error: never): MoveErrorCopy {
  throw new Error(`Unhandled API error: ${JSON.stringify(error)}`);
}
