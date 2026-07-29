/**
 * Presentation rules of the Equipo surface (`/team`), as pure functions.
 *
 * Framework-free on purpose: the page frontmatter renders the first paint with these
 * functions and the island patches the very same DOM with them after every refetch, so a
 * server-rendered row and a live-patched row cannot drift into two different sets of words,
 * tones or widths. Duplicating any of this in the island is the failure mode this module
 * exists to prevent.
 *
 * Nothing here decides *when* someone is overloaded: `is_overloaded` arrives from the API
 * (`utilization > 1.0`, `docs/API.md` §2.14) and is only ever rendered. A threshold invented
 * on this side would be a second definition of overload that no admin change could correct.
 */
import type { TeamLoadEntry } from "../../lib/api/domain";
import type { ApiErrorCode } from "../../lib/api/errors";

/**
 * The tone classes of `base.css` this surface uses. A row carries one (it drives the bar and
 * the overload marker through `--tone-solid`) and every chip carries its own, so a chip's
 * meaning never depends on the row it happens to sit in.
 */
export type ToneClass = "tone-piedra" | "tone-ambar" | "tone-rojo";

/** Every tone a row or chip can wear; the island removes all of them before adding one. */
export const TONE_CLASSES: readonly ToneClass[] = [
  "tone-piedra",
  "tone-ambar",
  "tone-rojo",
];

/**
 * Identity of one chip slot on a row.
 *
 * Slots are fixed and always rendered — hidden when they carry nothing — so the island
 * patches text and visibility instead of rebuilding markup that only Astro should own.
 */
export type ChipKey = "overload" | "open" | "overdue" | "blocked" | "urgent";

/** One chip of a row, resolved from the entry the API returned. */
export interface LoadChip {
  readonly key: ChipKey;
  readonly tone: ToneClass;
  /** Spanish, already pluralized; the only string the chip renders. */
  readonly text: string;
  /** False when the count is zero: a chip reading "0 vencidas" is noise, not a signal. */
  readonly visible: boolean;
}

/** Width of a full bar, in percent of its track. */
const FULL_BAR_PERCENT = 100;

/** Ratio at which the bar is full; above it the bar clamps and the figure keeps the truth. */
const FULL_RATIO = 1;

/** A row for the `<template>` the island clones when a person joins mid-session. */
export const BLANK_ENTRY: TeamLoadEntry = {
  alias: "",
  label: "",
  role: null,
  weekly_capacity_points: 0,
  load_points: 0,
  utilization: 0,
  is_overloaded: false,
  open_tasks: 0,
  blocked_tasks: 0,
  high_or_critical_open: 0,
  overdue_tasks: 0,
  projects_owned: 0,
};

/** Copy of the `empty` arm: what is empty, and what fills it. */
export const EMPTY_MESSAGE =
  "Todavía no hay nadie en el equipo. El roster llega con los datos semilla y se edita desde el panel de administración; en cuanto alguien tenga tareas asignadas, su carga aparece aquí.";

/**
 * Orders the roster the way the surface is read: the most loaded person first.
 *
 * The API answers ordered by display name (`read_team_load`), which is the wrong order for
 * the question this screen asks. Ties break on absolute points and then on the label, so two
 * people at the same utilization keep a stable position across refetches instead of swapping
 * places on every event.
 */
export function sortByLoad(
  entries: readonly TeamLoadEntry[],
): readonly TeamLoadEntry[] {
  return [...entries].sort(
    (left, right) =>
      right.utilization - left.utilization ||
      right.load_points - left.load_points ||
      left.label.localeCompare(right.label, "es"),
  );
}

/**
 * Width of the load bar, in percent of its track.
 *
 * `utilization` is a ratio (`load_points / weekly_capacity_points`), so `1` is a full bar and
 * `1.55` is an overloaded one. Above `1` the bar clamps while the figure beside it keeps the
 * true number: a bar allowed to overflow would state "very overloaded" with a geometry the
 * eye cannot compare across rows, and the ámbar tone plus the "Sobrecarga" chip already name
 * that condition in words.
 *
 * A negative or non-finite ratio — only reachable through a backend bug — renders as an empty
 * bar rather than as an invalid `width` the browser would silently drop.
 */
export function barPercent(utilization: number): number {
  if (!Number.isFinite(utilization) || utilization <= 0) return 0;
  const clamped = Math.min(utilization, FULL_RATIO) * FULL_BAR_PERCENT;
  return Math.round(clamped * 10) / 10;
}

/**
 * The utilization ratio as a percentage figure: `1.55` → `155 %`.
 *
 * The space before the sign is non-breaking, as Spanish typography sets it, so the number and
 * its unit can never wrap apart mid-row. Rounded to whole percent because the API already
 * rounds the ratio to two decimals and a tenth of a percent of somebody's week is not a
 * decision anyone makes.
 */
export function formatUtilization(utilization: number): string {
  const safe = Number.isFinite(utilization) ? utilization : 0;
  return `${Math.round(safe * FULL_BAR_PERCENT)} %`;
}

/**
 * Load against capacity as one tabular readout: `62 / 40 pts`.
 *
 * Both halves stay visible because the percentage alone hides the scale — 150 % of a 10-point
 * week and 150 % of a 40-point week are not the same staffing problem.
 */
export function formatPoints(
  loadPoints: number,
  capacityPoints: number,
): string {
  return `${loadPoints} / ${capacityPoints} pts`;
}

/**
 * The identity line under a person's name: their role and how many projects they own.
 *
 * A missing role is named ("Sin rol") rather than collapsed, so the line never renders as a
 * dangling separator and the reader can tell "no role recorded" from "role not shown".
 */
export function formatMeta(
  role: string | null | undefined,
  projectsOwned: number,
): string {
  const owned =
    projectsOwned === 0
      ? "sin proyectos a cargo"
      : `${plural(projectsOwned, "proyecto", "proyectos")} a cargo`;
  return `${role ?? "Sin rol"} · ${owned}`;
}

/**
 * Header readout of the whole roster: how many people, how many over capacity.
 *
 * Counts `is_overloaded` as the API reported it per person; it is a projection of the same
 * payload the rows render, never a second judgement made here.
 */
export function formatSummary(entries: readonly TeamLoadEntry[]): string {
  const people = plural(entries.length, "persona", "personas");
  const overloaded = entries.filter((entry) => entry.is_overloaded).length;
  if (overloaded === 0) return `${people} · nadie con sobrecarga`;
  return `${people} · ${overloaded} con sobrecarga`;
}

/**
 * Tone of one person's bar and overload marker.
 *
 * Piedra is "measured, nothing to say"; ámbar is the API's `is_overloaded`. Rojo is
 * deliberately absent here — being short-staffed is a staffing decision, not a failure of the
 * work (`docs/API.md` §2.14) — and is left to the chips that count late and blocked tasks.
 */
export function loadTone(isOverloaded: boolean): ToneClass {
  return isOverloaded ? "tone-ambar" : "tone-piedra";
}

/**
 * The fixed chip set of one row, in reading order.
 *
 * Every key is always returned so the caller can render fixed slots and the island can patch
 * them; `visible` decides which ones carry something today. A person with no open tasks gets
 * an ámbar chip naming that absence instead of an empty bar nobody can interpret
 * (`DESIGN.md`, The Present-Absence Rule).
 */
export function chipsFor(entry: TeamLoadEntry): readonly LoadChip[] {
  return [
    {
      key: "overload",
      tone: "tone-ambar",
      text: "Sobrecarga",
      visible: entry.is_overloaded,
    },
    openChip(entry.open_tasks),
    {
      key: "overdue",
      tone: "tone-rojo",
      text: plural(entry.overdue_tasks, "vencida", "vencidas"),
      visible: entry.overdue_tasks > 0,
    },
    {
      key: "blocked",
      tone: "tone-rojo",
      text: plural(entry.blocked_tasks, "bloqueada", "bloqueadas"),
      visible: entry.blocked_tasks > 0,
    },
    {
      key: "urgent",
      tone: "tone-ambar",
      text: plural(entry.high_or_critical_open, "urgente", "urgentes"),
      visible: entry.high_or_critical_open > 0,
    },
  ];
}

/**
 * Spanish copy for one failed read, chosen by `ApiError.code`.
 *
 * The client's `message` is English prose written for logs (`docs/API.md` §4.2), so it is
 * never rendered; this map is the surface's own vocabulary. Two codes that a read-only
 * endpoint cannot legitimately produce still get an answer, because a screen that renders
 * nothing when the impossible happens is worse than one that admits it.
 */
export function errorCopy(code: ApiErrorCode): string {
  return ERROR_COPY[code];
}

const ERROR_COPY: Record<ApiErrorCode, string> = {
  network_error:
    "No hubo respuesta de la API. Comprueba que el backend esté en marcha y vuelve a intentarlo.",
  not_found: "La API no reconoce la ruta de carga del equipo.",
  validation_error: "La API rechazó los parámetros de la consulta de carga.",
  transition_not_allowed:
    "La API respondió con un conflicto de transición, que una consulta de solo lectura no debería provocar.",
  conflicting_state:
    "La API respondió con un conflicto de estado, que una consulta de solo lectura no debería provocar.",
  unknown_error:
    "La API respondió con un error inesperado. Si acabas de volver, puede que tu sesión haya caducado.",
};

/**
 * The wall-clock hour of an instant, for the staleness marker: `14:32`.
 *
 * An unparseable timestamp is named rather than rendered as an empty slot, because the whole
 * point of the disconnected state is telling the operator *when* the data stopped moving.
 */
export function formatClock(iso: string): string {
  const at = new Date(iso);
  if (Number.isNaN(at.getTime())) return "hora desconocida";
  return new Intl.DateTimeFormat("es", {
    hour: "2-digit",
    minute: "2-digit",
  }).format(at);
}

/** The same instant in full, for the `title` of the clock readout. */
export function formatStamp(iso: string): string {
  const at = new Date(iso);
  if (Number.isNaN(at.getTime())) return "Instante desconocido";
  return new Intl.DateTimeFormat("es", {
    dateStyle: "long",
    timeStyle: "medium",
  }).format(at);
}

/** The "open tasks" slot, which is the one chip that also has to speak when the count is zero. */
function openChip(openTasks: number): LoadChip {
  if (openTasks === 0) {
    return {
      key: "open",
      tone: "tone-ambar",
      text: "Sin tareas abiertas",
      visible: true,
    };
  }
  return {
    key: "open",
    tone: "tone-piedra",
    text: plural(openTasks, "tarea abierta", "tareas abiertas"),
    visible: true,
  };
}

/** `3 tareas abiertas` / `1 tarea abierta` — the count always leads, the noun agrees. */
function plural(count: number, singular: string, many: string): string {
  return `${count} ${count === 1 ? singular : many}`;
}
