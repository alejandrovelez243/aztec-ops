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
 * - **The API owns legality, on both surfaces.** A project's legal moves are
 *   `project.transitions` and a task's are `task.transitions` (`docs/API.md` §2.2 and §2.8);
 *   this module never derives a move from a state code, so an edge added in the admin appears
 *   on the board with no frontend change, and one removed there stops being offered.
 * - **Legality is known before the drop, never after it.** Both surfaces read it from a `GET`
 *   and cache it per code, so `requires_reason` is already in hand when a card lands: the
 *   dialog opens and the reason travels with the first `POST`. Learning the rule from the
 *   refusal instead would mean the operator has to perform a legal move twice to perform it
 *   once, which is the same as not being able to perform it at all.
 * - **Nothing is painted optimistically.** {@link submitMove} and {@link submitTaskMove}
 *   return the server's answer; the caller keeps the card pending until the matching envelope
 *   arrives (`docs/standards/PATTERNS_FRONTEND.md` §8).
 */
import {
  getProject,
  getTask,
  postProjectTransition,
  postTaskTransition,
} from "../../lib/api/client";
import type { ApiError } from "../../lib/api/errors";
import type {
  ProjectDetail,
  TaskDetail,
  TaskItem,
  Transition,
} from "../../lib/api/domain";
import type { Result } from "../../lib/api/client";
import { fieldList, spanishList } from "./board-model";

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
    }
  | { readonly kind: "error"; readonly title: string; readonly detail: string };

/** A failure worded for a person: what was refused, and which rule refused it. */
export interface MoveErrorCopy {
  readonly title: string;
  readonly detail: string;
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

/**
 * Resolved legal moves per aggregate, keyed `kind:code`.
 *
 * One map for both surfaces because both answer the same question and are invalidated by the
 * same event — a state change — and the key prefix keeps a project and a task that happen to
 * share a code apart.
 */
const cache = new Map<string, MovesResult>();

/** In-flight lookups, so a fast second grab joins the first request instead of racing it. */
const inFlight = new Map<string, Promise<MovesResult>>();

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
  return resolve(projectKey(code), async () =>
    fromProject(await getProject(code), projectSubject(code)),
  );
}

/**
 * The legal moves of one task, cached the same way and for the same reason.
 *
 * `GET /api/v1/tasks/{code}` publishes the task's `transitions` with their labels, their
 * `requires_reason` and their `requires_fields`, which is everything the board needs to dim
 * an illegal column, name the rule on its lock chip and ask for a reason *before* posting.
 */
export async function loadTaskMoves(code: string): Promise<MovesResult> {
  return resolve(taskKey(code), async () =>
    fromTask(await getTask(code), taskSubject(code)),
  );
}

/** Forgets one project's legal moves; call it whenever its state changed. */
export function invalidateMoves(code: string): void {
  cache.delete(projectKey(code));
}

/** Forgets one task's legal moves; call it whenever its state changed. */
export function invalidateTaskMoves(code: string): void {
  cache.delete(taskKey(code));
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
 * Same contract as {@link submitMove}: the response proves the write, the envelope repaints,
 * and the cached legality is dropped first because a transition that lands makes it stale.
 */
export async function submitTaskMove(
  code: string,
  toStateCode: string,
  reason: string,
): Promise<Result<TaskItem>> {
  invalidateTaskMoves(code);
  return postTaskTransition(code, { to_state: toStateCode, reason });
}

/**
 * Words a refusal in Spanish, naming the domain rule that produced it.
 *
 * `error.message` is generated English prose for logs and is never shown (`docs/API.md`
 * §4.2); every branch here keys on `code` / `backendCode`, so a message reworded server-side
 * cannot change what the operator reads.
 *
 * `nameState` turns a workflow state code into the label the surface renders for it, so a
 * refused move can say what *is* possible in the operator's own vocabulary.
 */
export function moveErrorCopy(
  error: ApiError,
  subject: MoveSubject,
  targetLabel: string,
  nameState: (stateCode: string) => string,
): MoveErrorCopy {
  switch (error.kind) {
    case "transition_not_allowed":
      return {
        title: `${subject.code} no puede pasar a ${targetLabel}`,
        detail: allowedDetail(error.allowed.map(nameState)),
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
 * Names what the card *can* do, from the `allowed` list the 409 carried.
 *
 * A refusal that only says "no" leaves the operator aiming at columns until one takes the
 * card; the server already knows the answer and puts it in `details.allowed` (`docs/API.md`
 * §2.5), so the only thing left to do is say it out loud.
 */
function allowedDetail(stateLabels: readonly string[]): string {
  if (stateLabels.length === 0) {
    return "Desde su estado actual el flujo no permite ningún movimiento.";
  }
  return `Desde su estado actual solo puede pasar a ${spanishList(stateLabels)}.`;
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
 * One lookup, deduplicated and cached.
 *
 * `read` is a client call, and client calls never throw (`PATTERNS_FRONTEND.md` §3), so the
 * `error` arm is the only failure shape a caller has to handle. Only `ready` is cached: a
 * refused or offline lookup must be retried by the next grab, not remembered as an answer.
 */
async function resolve(
  key: string,
  read: () => Promise<MovesResult>,
): Promise<MovesResult> {
  const cached = cache.get(key);
  if (cached !== undefined) return cached;
  const running = inFlight.get(key);
  if (running !== undefined) return running;

  const request = read()
    .then((resolved) => {
      if (resolved.kind === "ready") cache.set(key, resolved);
      return resolved;
    })
    .finally(() => {
      inFlight.delete(key);
    });
  inFlight.set(key, request);
  return request;
}

function projectKey(code: string): string {
  return `project:${code}`;
}

function taskKey(code: string): string {
  return `task:${code}`;
}

/**
 * Words a failed *lookup* — asking an aggregate what it may do, not asking it to do
 * something.
 *
 * Kept apart from {@link moveErrorCopy} because the two failures are different sentences: one
 * says a move was refused, the other says we never learned which moves exist.
 */
function lookupErrorCopy(error: ApiError, subject: MoveSubject): MoveErrorCopy {
  switch (error.kind) {
    case "network":
      return {
        title: "Sin conexión con el servidor",
        detail: `No pudimos consultar qué movimientos admite ${subject.noun}. Vuelve a intentarlo.`,
      };
    case "not_found":
      return {
        title: `${subject.code} ya no existe`,
        detail: `Actualiza el tablero para ver ${subject.noun} tal como está en el servidor.`,
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
        detail: `El servidor no devolvió las transiciones legales de ${subject.code}, así que el tablero no puede ofrecer ninguna.`,
      };
  }
}

/** A project's answer: its transitions, and which of their required fields it lacks. */
function fromProject(
  result: Result<ProjectDetail>,
  subject: MoveSubject,
): MovesResult {
  if (!result.ok) return errorResult(result.error, subject);
  const project = result.data;
  return toMovesResult(project.transitions, project.state.label, {
    name: project.name,
    summary: project.summary ?? null,
    next_step: project.next_step ?? null,
    target_date: project.target_date ?? null,
    start_date: project.start_date ?? null,
    business_value: project.business_value ?? null,
    owner: project.owner ?? null,
    stage: project.stage ?? null,
    project_type: project.project_type ?? null,
  });
}

/** A task's answer, read exactly the same way — same shape, same rules, different endpoint. */
function fromTask(
  result: Result<TaskDetail>,
  subject: MoveSubject,
): MovesResult {
  if (!result.ok) return errorResult(result.error, subject);
  const task = result.data;
  return toMovesResult(task.transitions, task.state.label, {
    title: task.title,
    detail: task.detail,
    assignee: task.assignee ?? null,
    due_date: task.due_date ?? null,
    last_progress: task.last_progress,
    priority: task.priority,
  });
}

function errorResult(error: ApiError, subject: MoveSubject): MovesResult {
  const copy = lookupErrorCopy(error, subject);
  return { kind: "error", title: copy.title, detail: copy.detail };
}

/**
 * The `ready` arm: every transition the aggregate reported, plus whether the data it holds
 * today satisfies each one.
 */
function toMovesResult(
  transitions: readonly Transition[],
  stateLabel: string,
  values: Readonly<Record<string, unknown>>,
): MovesResult {
  const empty = emptyAttributes(transitions, values);
  return {
    kind: "ready",
    stateLabel,
    options: transitions.map((transition) => ({
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
 * The attributes `requires_fields` names, and whether each is currently empty.
 *
 * An attribute the frontend does not know about counts as empty: the control then renders
 * dead and names the field, which is the honest answer to a contract that grew server-side —
 * the opposite mistake enables a move the API is about to refuse.
 */
function emptyAttributes(
  transitions: readonly Transition[],
  values: Readonly<Record<string, unknown>>,
): ReadonlySet<string> {
  const empty = new Set<string>();
  for (const transition of transitions) {
    for (const field of transition.requires_fields) {
      const value = values[field];
      if (value === undefined || value === null || value === "") {
        empty.add(field);
      }
    }
  }
  return empty;
}

/**
 * A payload the server refused.
 *
 * The `reason` branch names a motive the server would not take, never an instruction to try
 * again: the board asks for the reason before it posts, so "vuelve a intentarlo" would send
 * the operator back around a loop that already ran.
 */
function validationDetail(
  fields: Record<string, string[]>,
  noun: string,
): string {
  const names = Object.keys(fields);
  if (names.length === 0) {
    return "El servidor rechazó los datos del movimiento.";
  }
  if (names.includes("reason")) {
    return "El servidor no aceptó el motivo de esta transición.";
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
