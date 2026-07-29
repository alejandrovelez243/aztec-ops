/**
 * Every word the Resumen surface says, in one file.
 *
 * Routes, files, classes and data attributes are English (CLAUDE.md §Language);
 * everything a person reads is Spanish. Keeping the sentences here — rather than
 * inlined across a renderer that also decides layout — is what makes "is this
 * surface fully translated?" a question a reviewer can answer by reading one
 * file.
 *
 * `error.message` from the API is English prose written for logs. It is never
 * rendered: the mapping below turns `error.code` into words an operator can act
 * on, which is the contract `docs/API.md` §1.5 asks callers to honour.
 */

import type { ApiErrorCode } from "../../lib/api/errors";
import { assertNever } from "../../lib/view-state";
import type { DueState, ZoneKey } from "./model";

/** One zone's heading and the sentence shown when it holds nothing. */
export interface ZoneCopy {
  readonly key: ZoneKey;
  /** Rendered uppercase by `.label`; written here in sentence case. */
  readonly label: string;
  /** What this tier means, one line, under the heading. */
  readonly hint: string;
}

/** The three zones of comp C, in reading order. */
export const ZONES: readonly ZoneCopy[] = [
  {
    key: "today",
    label: "Atender hoy",
    hint: "Lo más alto de la cola: decide aquí antes de abrir nada más.",
  },
  {
    key: "week",
    label: "Esta semana",
    hint: "Sigue pesando, pero no exige tu mañana.",
  },
  {
    key: "radar",
    label: "Radar",
    hint: "El resto de la cartera, en orden de puntaje.",
  },
];

/** Text of the due chip, from the closed set of due cases. */
export function dueText(due: DueState): string {
  switch (due.kind) {
    case "overdue":
      return `vencido hace ${due.days} d`;
    case "today":
      return "vence hoy";
    case "ahead":
      return `vence en ${due.days} d`;
    case "missing":
      return "sin fecha";
    default:
      return assertNever(due);
  }
}

/**
 * Tone class of the due chip.
 *
 * Rojo is a fact that already happened, ámbar is a fact that needs a decision
 * today (including the absent date, which is the `NO_TARGET_DATE` risk wearing
 * the Present-Absence Rule), piedra is a date that is simply in the future.
 */
export function dueTone(due: DueState): string {
  switch (due.kind) {
    case "overdue":
      return "tone-rojo";
    case "today":
    case "missing":
      return "tone-ambar";
    case "ahead":
      return "tone-piedra";
    default:
      return assertNever(due);
  }
}

/** `1 bloqueo abierto` / `3 bloqueos abiertos`, agreeing in number. */
export function blockersText(count: number): string {
  return count === 1 ? "1 bloqueo abierto" : `${count} bloqueos abiertos`;
}

/** `1 tarea abierta` / `4 tareas abiertas`. */
export function openTasksText(count: number): string {
  return count === 1 ? "1 tarea abierta" : `${count} tareas abiertas`;
}

/** `1 proyecto` / `5 proyectos`, for the owner-load teaser's second line. */
export function ownedProjectsText(count: number): string {
  return count === 1 ? "1 proyecto" : `${count} proyectos`;
}

/**
 * What failed, in words an operator can act on.
 *
 * Exhaustive over `ApiErrorCode`: a code added to the union stops compiling here
 * instead of reaching a screen as a bare machine string.
 */
export function failureText(code: ApiErrorCode): string {
  switch (code) {
    case "network_error":
      return "No hubo respuesta de la API. Puede estar reiniciándose o no ser alcanzable desde aquí.";
    case "not_found":
      return "La API respondió que este recurso no existe.";
    case "validation_error":
      return "La API rechazó los parámetros de la consulta.";
    case "transition_not_allowed":
      return "La API rechazó la operación por una regla de flujo de trabajo.";
    case "conflicting_state":
      return "El estado en la API ya no es el que teníamos a la vista.";
    case "domain_error":
      return "Una regla de negocio rechazó la operación.";
    case "authentication_required":
      return "Tu sesión no viajó con la petición. Vuelve a iniciar sesión para ver la cartera.";
    case "invalid_token":
      return "Tu sesión caducó. Estamos reintentando con credenciales renovadas.";
    case "invalid_credentials":
      return "Las credenciales no son válidas. Inicia sesión de nuevo.";
    case "permission_denied":
      return "Te falta la capacidad de líder de operaciones para esta acción.";
    case "unknown_error":
      return "La API respondió con un error inesperado.";
    default:
      return assertNever(code);
  }
}

/** Copy of the queue region's four states, kept beside the zones they replace. */
export const QUEUE_COPY = {
  emptyTitle: "Todavía no hay cola",
  emptyBody:
    "Ningún proyecto tiene puntaje calculado. Registra el primero en la administración y aparecerá aquí, ordenado por lo que más pesa.",
  emptyAction: "Ver proyectos",
  errorTitle: "No pudimos cargar la cola de prioridades",
  errorAction: "Reintentar",
  loadingLabel: "Cargando la cola de prioridades",
} as const;

/** Copy of the owner-load teaser's four states. */
export const TEAM_COPY = {
  title: "Carga del equipo",
  link: "Ver equipo",
  emptyTitle: "Nadie en el roster",
  emptyBody:
    "Ninguna persona activa tiene trabajo asignado, así que no hay carga que repartir todavía.",
  errorTitle: "No pudimos cargar la carga del equipo",
  errorAction: "Reintentar",
  loadingLabel: "Cargando la carga del equipo",
  overloaded: "Sobrecarga",
} as const;

/** Copy of the live-stream banner the island reveals when the stream drops. */
export const STREAM_COPY = {
  staleTitle: "Sin conexión en vivo",
  staleBody: "Sigues viendo los últimos datos recibidos",
  staleNoEvents: "todavía sin eventos en esta sesión",
  lastEventPrefix: "último evento",
  reconnect: "Reconectar",
} as const;

/** Copy shared by the cards themselves. */
export const CARD_COPY = {
  scoreCaption: "Puntaje",
  noBreakdown: "sin desglose",
  noNextStep: "sin próximo paso",
  manualOverride: "Ajuste manual",
  open: "Ver proyecto",
  unassigned: "sin responsable",
  systemActor: "Sistema",
  priorityMoved: "Prioridad recalculada",
} as const;

/** Copy of the page header. */
export const PAGE_COPY = {
  title: "Resumen",
  crumb: "Inicio",
  lead: "Tu muro de hoy: lo que pide atención primero, con el argumento que lo puso ahí.",
} as const;

/*
 * The "Última actividad" region used to be a stated absence here, because the
 * API published no portfolio-wide feed. `GET /api/v1/activity` landed; the copy
 * now lives with the surface that owns the trail, in
 * `src/components/activity/copy.ts` (`TEASER_COPY`).
 */
