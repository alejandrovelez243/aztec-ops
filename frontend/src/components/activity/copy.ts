/**
 * Every word the Actividad surface says, in one file.
 *
 * Routes, files, classes and data attributes are English (CLAUDE.md §Language);
 * everything a person reads is Spanish. Keeping the sentences here — rather than
 * inlined across components that also decide layout — makes "is this surface
 * fully translated?" a question a reviewer answers by reading one file.
 *
 * `ApiError.message` is generated English prose for logs (`docs/API.md` §1.5)
 * and is never rendered: the error arm names the failure in its own words and
 * offers the read again.
 */

/** The page header. */
export const PAGE_COPY = {
  title: "Actividad",
  crumbHome: "Resumen",
  lead: "Todo lo que se movió en la cartera, de lo más reciente a lo más antiguo. Nada se edita ni se borra: esta bitácora es la prueba de qué pasó y quién lo hizo.",
} as const;

/** The filter bar. There is no "apply": every control filters on change. */
export const FILTER_COPY = {
  entityType: "Tipo",
  entityId: "Código",
  entityIdPlaceholder: "PRJ-22",
  verb: "Qué cambió",
  origin: "Origen",
  actor: "Quién",
  range: "Rango de fechas",
  since: "Desde",
  until: "Hasta",
  any: "Todo",
  clear: "Limpiar filtros",
  summaryOne: "1 filtro aplicado",
  summaryMany: "filtros aplicados",
  /** The actor entry that is not a person: the engine writes records too. */
  systemMeta: "Cambios que escribió la plataforma",
  anyActorMeta: "Cualquier persona y el sistema",
  rosterLoading: "Cargando el equipo…",
  rosterEmpty: "Todavía no hay nadie en el equipo.",
  loadTasksOne: "1 tarea abierta",
  loadTasksMany: "tareas abiertas",
} as const;

/** The four view states of the feed region. */
export const FEED_COPY = {
  loadingLabel: "Cargando la actividad de la cartera",
  emptyTitle: "Todavía no hay actividad",
  emptyBody:
    "En cuanto alguien cambie un estado, levante un bloqueo o el motor recalcule una prioridad, aparecerá aquí.",
  emptyFilteredTitle: "No hay actividad con estos filtros",
  emptyFilteredBody:
    "Ningún registro coincide con lo que pediste. Quita algún filtro o amplía el rango de fechas.",
  emptyFilteredAction: "Ver toda la actividad",
  errorTitle: "No pudimos cargar la actividad",
  errorAction: "Reintentar",
  staleTitle: "Sin conexión en vivo",
  staleBody: "Sigues viendo los últimos registros recibidos",
  staleNoEvents: "todavía sin eventos en esta sesión",
  staleLastEvent: "último evento",
  reconnect: "Reconectar",
} as const;

/** Column headings of the feed table. */
export const TABLE_COPY = {
  caption: "Registros de la cartera, del más reciente al más antiguo",
  colChange: "Qué cambió",
  colSubject: "Sobre qué",
  colDelta: "El cambio",
  colWho: "Quién y cuándo",
  colDecision: "Decisión",
} as const;

/** One row and the decision it may belong to. */
export const ROW_COPY = {
  decision: "Una decisión",
  decisionHint:
    "Estos registros se escribieron juntos: son un solo movimiento.",
  seeDecision: "Ver la decisión completa",
  /** The same link inside a table cell, where its column is already named. */
  seeDecisionShort: "Ver decisión",
  reasonLabel: "Motivo",
  viewingDecision: "Estás viendo una sola decisión",
  viewingDecisionBody:
    "Todos los registros que comparten este identificador de correlación.",
  clearDecision: "Volver a toda la actividad",
} as const;

/** The pager under the feed. */
export const PAGER_COPY = {
  previous: "Anterior",
  next: "Siguiente",
  label: "Paginación de la actividad",
} as const;

/** The compact "Última actividad" widget on Resumen. */
export const TEASER_COPY = {
  title: "Última actividad",
  link: "Ver toda",
  emptyTitle: "Sin movimientos todavía",
  emptyBody: "Los cambios de la cartera aparecerán aquí en cuanto ocurran.",
  errorTitle: "No pudimos cargar la actividad",
  errorBody: "Vuelve a cargar la página para reintentarlo.",
  loadingLabel: "Cargando la última actividad",
} as const;

/** `1 registro` / `4 registros`, agreeing in number. */
export function recordsText(count: number): string {
  return count === 1 ? "1 registro" : `${count} registros`;
}

/** How many facets are narrowing the feed, as a sentence or `""` for none. */
export function filterSummaryText(count: number): string {
  if (count === 0) return "";
  if (count === 1) return FILTER_COPY.summaryOne;
  return `${count} ${FILTER_COPY.summaryMany}`;
}
