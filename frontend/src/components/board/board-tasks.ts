/**
 * The task board's use case: read one project's tasks and project them onto cards.
 *
 * The surface's second read, and the only one the island performs on selection. It exists
 * beside `board-moves.ts` for the same reason that one does: the board has four callers —
 * a click on a project, a restored `?project=` on mount, a retry, and a `task.*` envelope
 * refreshing what is already open — and copy, claiming and race handling must be decided once.
 *
 * The cards are *not* grouped here. Columns are the TASK workflow's states, in the operator's
 * order, rendered whether or not anybody occupies them (`board-model.ts`); this module only
 * says which card exists and which state code it claims, and the island places each one in the
 * column that already exists for it.
 *
 * Two invariants:
 *
 * - **Never throws.** Every failure resolves to the `error` arm carrying Spanish copy keyed
 *   on `ApiError.code`; `error.message` is generated English prose for logs (`docs/API.md`
 *   §4.2) and is never rendered.
 * - **The newest selection wins.** Selections are faster than requests, so callers pass the
 *   code back into {@link isCurrent} before painting: a slow answer for a project the operator
 *   already left must not overwrite the board they are looking at.
 */
import { getProjectTasks } from "../../lib/api/client";
import type { ApiError } from "../../lib/api/errors";
import { toTaskCard, type TaskCardModel } from "./board-model";

/**
 * One page big enough to hold a project's whole task list.
 *
 * The server clamps to its own maximum, and the board is not a paginated surface: the
 * question it answers is "what is left on this project", which half an answer cannot address.
 */
const TASK_PAGE_SIZE = 200;

/** What the board learned when it asked a project for its tasks. */
export type TaskBoardResult =
  | { readonly kind: "ready"; readonly cards: readonly TaskCardModel[] }
  | { readonly kind: "empty" }
  | { readonly kind: "error"; readonly title: string; readonly detail: string };

/** The selection the board is currently painting for; `null` when nothing is selected. */
let currentCode: string | null = null;

/**
 * Claims the board for one project, invalidating every answer still in flight for another.
 *
 * Call it the instant the selection changes — before the request departs — so a response that
 * arrives late is dropped by {@link isCurrent} rather than painted over the newer choice.
 */
export function claimBoard(code: string | null): void {
  currentCode = code;
}

/** Whether `code` is still the project the board is showing. */
export function isCurrent(code: string): boolean {
  return currentCode === code;
}

/**
 * Reads one project's tasks and projects them onto cards.
 *
 * `now` is passed in rather than read here so the due chips of the tasks and of the projects
 * are measured against the same instant; two clocks in one screen is how a task reads "hoy"
 * beside a project that reads "+1 d".
 */
export async function loadTasks(
  code: string,
  now: Date,
): Promise<TaskBoardResult> {
  const result = await getProjectTasks(code, { page_size: TASK_PAGE_SIZE });
  if (!result.ok) return errorResult(result.error);
  const tasks = result.data.items;
  if (tasks.length === 0) return { kind: "empty" };
  return { kind: "ready", cards: tasks.map((task) => toTaskCard(task, now)) };
}

/**
 * Words a failed task read in Spanish, naming what is missing rather than what broke.
 *
 * Branches on `code` only, so a message reworded server-side cannot change what an operator
 * reads (`docs/API.md` §4.2).
 */
function errorResult(error: ApiError): TaskBoardResult {
  switch (error.kind) {
    case "network":
      return {
        kind: "error",
        title: "Sin conexión con el servidor",
        detail:
          "No pudimos leer las tareas de este proyecto. Vuelve a intentarlo.",
      };
    case "not_found":
      return {
        kind: "error",
        title: "El proyecto ya no está en el portafolio",
        detail: "Actualiza el tablero para ver la verdad del servidor.",
      };
    case "auth":
      return {
        kind: "error",
        title: "Tu sesión ya no es válida",
        detail: "Vuelve a iniciar sesión para ver las tareas.",
      };
    case "permission_denied":
      return {
        kind: "error",
        title: "No tienes permiso para ver estas tareas",
        detail: "Pide acceso a quien administra el portafolio.",
      };
    case "validation":
    case "transition_not_allowed":
    case "unknown":
    default:
      return {
        kind: "error",
        title: "No pudimos cargar las tareas",
        detail:
          "El servidor respondió con un error inesperado. Vuelve a intentarlo.",
      };
  }
}
