/**
 * Presentation rules of the Equipo surface (`/team`), as pure functions.
 *
 * Framework-free on purpose: the page frontmatter renders the first paint with these functions
 * and the island rebuilds the very same rows with them after every refetch, so a
 * server-rendered row and a live-refetched row cannot drift into two different sets of words,
 * tones or widths.
 *
 * Nothing here decides *when* someone is overloaded: `is_overloaded` arrives from the API
 * (`utilization > 1.0`, `docs/API.md` §2.14) and is only ever rendered. A threshold invented on
 * this side would be a second definition of overload that no admin change could correct.
 *
 * Nothing here filters or sorts either. Both are the server's — `GET /api/v1/team/load` takes
 * `q`, `role`, `status`, `overloaded` and `order_by` — so what the toolbar shows and what the
 * URL says are the same question, answered once.
 */
import type { TeamLoadEntry } from "../../lib/api/domain";
import { errorDetailByCode } from "../../lib/api/error-copy";
import type { ApiErrorCode } from "../../lib/api/errors";

/**
 * The tone classes of `base.css` this surface uses. The load cell carries one (it drives the
 * bar through `--tone-solid`) and every count chip carries its own, so a figure's meaning never
 * depends on the row it happens to sit in.
 */
export type ToneClass = "tone-piedra" | "tone-ambar" | "tone-rojo";

/** Every tone a cell can wear; the island removes all of them before adding one. */
export const TONE_CLASSES: readonly ToneClass[] = [
  "tone-piedra",
  "tone-ambar",
  "tone-rojo",
];

/** Which half of the roster is being read. Mirrors the API's `status` parameter exactly. */
export type RosterStatus = "active" | "inactive" | "all";

/**
 * One sortable column, as the wire names it. A literal union rather than `string`: a header
 * whose key the API would reject with a 422 does not compile, which is the only way a sort
 * control and its endpoint stay in step.
 */
export type SortField =
  | "label"
  | "role"
  | "utilization"
  | "load_points"
  | "capacity"
  | "open_tasks"
  | "overdue_tasks"
  | "blocked_tasks"
  | "urgent_tasks"
  | "projects_owned";

/** The question the toolbar and the URL both describe. */
export interface RosterQuery {
  /** Free text over the person's code and display name. Empty means no search. */
  readonly search: string;
  /** A single `catalog.Role` code, or `null` for every role. */
  readonly role: string | null;
  readonly status: RosterStatus;
  /** `null` is everybody, `true` is over capacity, `false` is who has room. */
  readonly overloaded: boolean | null;
  readonly sort: SortField;
  readonly descending: boolean;
}

/**
 * What the surface opens on: the whole active roster, most loaded first.
 *
 * The roster is read to find who is drowning, so it opens on that rather than on the alphabet.
 */
export const DEFAULT_QUERY: RosterQuery = {
  search: "",
  role: null,
  status: "active",
  overloaded: null,
  sort: "utilization",
  descending: true,
};

/** Width of a full bar, in percent of its track. */
const FULL_BAR_PERCENT = 100;

/** Ratio at which the bar is full; above it the bar clamps and the figure keeps the truth. */
const FULL_RATIO = 1;

/** Every sortable column, so a wire value read back from the URL can be validated. */
const SORT_FIELDS: readonly SortField[] = [
  "label",
  "role",
  "utilization",
  "load_points",
  "capacity",
  "open_tasks",
  "overdue_tasks",
  "blocked_tasks",
  "urgent_tasks",
  "projects_owned",
];

/** Every status, for the same reason. */
const STATUSES: readonly RosterStatus[] = ["active", "inactive", "all"];

/**
 * Copy of the `empty` arm: what fills it. The heading beside it names what is empty, so this
 * sentence carries only the action.
 */
export const EMPTY_MESSAGE =
  "Nadie coincide con lo que estás buscando. Cambia los filtros, o añade a alguien al equipo.";

/** Copy of the `empty` arm when nothing is filtered — a roster that has never been seeded. */
export const EMPTY_ROSTER_MESSAGE =
  "Todavía no hay nadie en el equipo. Añade a la primera persona para empezar a repartir trabajo.";

/**
 * Reads the question out of a URL's query string, ignoring anything it does not understand.
 *
 * A bad `order_by` is dropped rather than sent on: the server would answer 422 and the operator
 * would see an error arm for a link somebody mistyped, when showing them the default roster is
 * both correct and useful. Everything the *toolbar* can produce is valid by construction — this
 * leniency is for hand-edited and stale URLs only.
 */
export function queryFromParams(params: URLSearchParams): RosterQuery {
  const rawSort = params.get("order_by") ?? "";
  const descending = rawSort.startsWith("-");
  const sort = rawSort.replace(/^-/, "");
  const status = params.get("status") ?? "";
  const overloaded = params.get("overloaded");

  return {
    search: params.get("q") ?? "",
    role: params.get("role") || null,
    status: isStatus(status) ? status : DEFAULT_QUERY.status,
    overloaded: overloaded === null ? null : overloaded === "true",
    sort: isSortField(sort) ? sort : DEFAULT_QUERY.sort,
    descending: isSortField(sort) ? descending : DEFAULT_QUERY.descending,
  };
}

/**
 * The query as the API takes it. Defaults are omitted, so the common case is a bare URL.
 *
 * Omission is not a shortcut: a URL that spells out every default cannot be told apart from one
 * where somebody chose them, which matters the moment the defaults change.
 */
export function paramsFromQuery(query: RosterQuery): URLSearchParams {
  const params = new URLSearchParams();
  if (query.search !== "") params.set("q", query.search);
  if (query.role !== null) params.set("role", query.role);
  if (query.status !== DEFAULT_QUERY.status) params.set("status", query.status);
  if (query.overloaded !== null) {
    params.set("overloaded", String(query.overloaded));
  }
  const order = orderByOf(query);
  if (order !== orderByOf(DEFAULT_QUERY)) params.set("order_by", order);
  return params;
}

/** The signed field name the API's `order_by` takes: `utilization` or `-utilization`. */
export function orderByOf(query: RosterQuery): string {
  return `${query.descending ? "-" : ""}${query.sort}`;
}

/**
 * Whether the reader is looking at a filtered view.
 *
 * Drives which empty copy is shown: "nobody matches your filters" and "nobody is on the team"
 * are different facts, and offering to clear filters that are not set is how a screen tells
 * somebody their data is missing when it is only hidden.
 */
export function isFiltered(query: RosterQuery): boolean {
  return (
    query.search !== "" ||
    query.role !== null ||
    query.status !== DEFAULT_QUERY.status ||
    query.overloaded !== null
  );
}

/**
 * What clicking a sortable header produces.
 *
 * Clicking the active column flips its direction; clicking a new one adopts that column's
 * natural direction — names read A→Z, figures read biggest-first, because the reason to sort by
 * "vencidas" is to find who has the most of them.
 */
export function toggledSort(query: RosterQuery, field: SortField): RosterQuery {
  if (query.sort === field) return { ...query, descending: !query.descending };
  return {
    ...query,
    sort: field,
    descending: field !== "label" && field !== "role",
  };
}

/**
 * Width of the load bar, in percent of its track.
 *
 * `utilization` is a ratio (`load_points / weekly_capacity_points`), so `1` is a full bar and
 * `1.55` is an overloaded one. Above `1` the bar clamps while the figure beside it keeps the
 * true number: a bar allowed to overflow would state "very overloaded" with a geometry the eye
 * cannot compare across rows, and the ámbar tone already names that condition.
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
 * its unit can never wrap apart mid-row.
 */
export function formatUtilization(utilization: number): string {
  const safe = Number.isFinite(utilization) ? utilization : 0;
  return `${Math.round(safe * FULL_BAR_PERCENT)}\u00A0%`;
}

/**
 * Load against capacity as one tabular readout: `28 / 20`.
 *
 * Both halves stay visible because the percentage alone hides the scale — 150 % of a 10-point
 * week and 150 % of a 40-point week are not the same staffing problem.
 */
export function formatPoints(
  loadPoints: number,
  capacityPoints: number,
): string {
  return `${loadPoints} / ${capacityPoints}`;
}

/**
 * A count as a table cell: the number, or an em dash when there is nothing to count.
 *
 * A dash rather than a `0`, because a column of zeros is visual noise that hides the figures
 * that matter, and an empty cell would read as missing data instead of as an absence.
 */
export function formatCount(count: number): string {
  return count === 0 ? "—" : String(count);
}

/** Tone of a count of *late or blocked* work: rojo the moment there is any. */
export function alertTone(count: number): ToneClass {
  return count > 0 ? "tone-rojo" : "tone-piedra";
}

/** Tone of a count of urgent open work: ámbar, which is attention rather than failure. */
export function urgentTone(count: number): ToneClass {
  return count > 0 ? "tone-ambar" : "tone-piedra";
}

/**
 * Tone of one person's load bar.
 *
 * Piedra is "measured, nothing to say"; ámbar is the API's `is_overloaded`. Rojo is deliberately
 * absent — being short-staffed is a staffing decision, not a failure of the work
 * (`docs/API.md` §2.14) — and is left to the columns that count late and blocked tasks.
 */
export function loadTone(isOverloaded: boolean): ToneClass {
  return isOverloaded ? "tone-ambar" : "tone-piedra";
}

/** The role to render, naming its absence rather than leaving a blank cell. */
export function formatRole(role: string | null | undefined): string {
  return role ?? "Sin rol";
}

/**
 * The status cell: whether the person takes work, and whether they can sign in.
 *
 * "Sin acceso" is stated beside "Activa" rather than instead of it, because they are
 * independent facts: a seeded person is a real assignee who has simply never been given a
 * password, and reading that as "inactive" would hide somebody who is carrying tasks today.
 */
export function formatStatus(entry: TeamLoadEntry): string {
  if (!entry.is_active) return "Retirada";
  return entry.has_password ? "Activa" : "Activa · sin acceso";
}

/** Tone of the status cell: a retired person is quiet, a person with no access is ámbar. */
export function statusTone(entry: TeamLoadEntry): ToneClass {
  if (!entry.is_active) return "tone-piedra";
  return entry.has_password ? "tone-piedra" : "tone-ambar";
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
 * Spanish copy for one failed read or write, chosen by `ApiError.code`.
 *
 * The client's `message` is English prose written for logs (`docs/API.md` §4.2), so it is never
 * rendered; this map is the surface's own vocabulary.
 */
export function errorCopy(code: ApiErrorCode): string {
  return errorDetailByCode(code);
}

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

/** `3 personas` / `1 persona` — the count always leads, the noun agrees. */
function plural(count: number, singular: string, many: string): string {
  return `${count} ${count === 1 ? singular : many}`;
}

function isSortField(value: string): value is SortField {
  return (SORT_FIELDS as readonly string[]).includes(value);
}

function isStatus(value: string): value is RosterStatus {
  return (STATUSES as readonly string[]).includes(value);
}
