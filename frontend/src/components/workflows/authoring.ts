/**
 * The vocabulary and the refusals of the workflow editor: what a form may offer, and what a
 * rejected write means in Spanish.
 *
 * Pure TypeScript — no DOM, no network — so the pickers rendered on the server and the messages
 * written by the island read from one source. Nothing here is keyed by a *business* code: the
 * two maps below are **structural** vocabularies (adding a value to either is a migration, not a
 * fixture row, `docs/DATA_MODEL.md` §12), which is exactly the distinction
 * `docs/standards/PATTERNS_FRONTEND.md` §7 draws — a state's own `code` is the operator's and is
 * never mapped, while `applies_to` and `category` are the language the schema itself is written
 * in.
 *
 * The failure copy is the reason this module exists rather than a handful of literals at the
 * call sites. Every write on this surface can be refused by a rule the operator cannot see, and
 * the shared `errorCopy` answers `conflicting_state` with "vuelve a cargar para ver el estado
 * actual" — true of a blocker somebody else resolved, useless here. A retirement refused because
 * three projects are standing on the state has exactly one useful sentence, and it contains the
 * number three.
 */
import { errorCopy, type ErrorCopy } from "../../lib/api/error-copy";
import type { ApiError } from "../../lib/api/errors";
import { categoryLabel } from "../projects/tone";

/** One choosable value of a closed vocabulary, shaped for `MenuSelect`. */
export interface Choice {
  readonly value: string;
  readonly label: string;
}

/**
 * The kinds of record a lifecycle can govern.
 *
 * Closed and structural: `Workflow.applies_to` accepts these two and a third would be a
 * migration, so naming them in Spanish is safe in a way that naming a state code never is.
 */
export const APPLIES_TO_CHOICES: readonly Choice[] = [
  { value: "PROJECT", label: "Proyectos" },
  { value: "TASK", label: "Tareas" },
];

/**
 * The five state categories, in lifecycle order.
 *
 * This is the value every risk rule and every priority signal branches on, which is why the set
 * is closed: a sixth entry would be an inert state rather than a new behaviour, and the API
 * rejects one. The Spanish comes from `categoryLabel`, the same function the project filters
 * render with, so a category cannot read one way here and another way there.
 */
export const CATEGORY_CHOICES: readonly Choice[] = [
  "BACKLOG",
  "IN_PROGRESS",
  "BLOCKED",
  "DONE",
  "CANCELLED",
].map((code) => ({ value: code, label: categoryLabel(code) }));

/**
 * The colours a state may be given, as **token names** rather than values.
 *
 * A hex literal may not appear outside `styles/tokens.css`
 * (`docs/standards/PATTERNS_FRONTEND.md` §10), and the operator picking a colour is picking a
 * meaning from the semantic set rather than an arbitrary paint — so the swatches render as
 * `var(--<token>)` and {@link tokenColor} reads the value back at submit time. A palette of five
 * is also the honest offer: `DESIGN.md` gives semantic colour exactly five meanings, and a free
 * colour wheel would let an operator paint a blocked state green. `""` is a real choice, not a
 * missing one: the state then renders in its category's tone.
 */
export const COLOR_CHOICES: readonly Choice[] = [
  { value: "", label: "El de su categoría" },
  { value: "rojo", label: "Rojo" },
  { value: "ambar", label: "Ámbar" },
  { value: "verde", label: "Verde" },
  { value: "cielo", label: "Cielo" },
  { value: "piedra", label: "Piedra" },
];

/** The token names of {@link COLOR_CHOICES}, without the "no colour" entry. */
const COLOR_TOKENS: readonly string[] = COLOR_CHOICES.map(
  (choice) => choice.value,
).filter((token) => token !== "");

/**
 * The `#RRGGBB` behind a design token, read from the document.
 *
 * Browser-only, and deliberately so: the value lives in `tokens.css` and this is the only way to
 * send it to an API without transcribing it into TypeScript. A token the document does not
 * define returns `""`, which the API reads as "no colour" — the same as choosing none, and
 * better than posting a broken value that its `^(#[0-9a-fA-F]{6})?$` would reject anyway.
 */
export function tokenColor(token: string): string {
  if (token === "") return "";
  const value = getComputedStyle(document.documentElement)
    .getPropertyValue(`--${token}`)
    .trim();
  return /^#[0-9a-fA-F]{6}$/.test(value) ? value : "";
}

/**
 * The token whose value is `color`, or `""` when none of them is.
 *
 * The inverse of {@link tokenColor}, for pre-selecting the swatch of a state that already has a
 * colour. A colour set in the admin outside the palette selects no swatch rather than the
 * nearest one: silently rounding somebody's choice to a different colour on open, and then
 * saving it on submit, is a write the operator never asked for.
 */
export function colorToken(color: string | null | undefined): string {
  if (color === null || color === undefined || color === "") return "";
  const wanted = color.toLowerCase();
  return COLOR_TOKENS.find((token) => tokenColor(token) === wanted) ?? "";
}

/**
 * Splits what the operator typed into field names.
 *
 * Commas, spaces and newlines all separate, because all three are what people type into a list
 * of identifiers. The entries are **not** validated against a vocabulary here: `requires_fields`
 * names attributes of a project or a task, the set grows on the backend, and a frontend allowlist
 * would silently drop the field somebody added this morning.
 */
export function parseFieldNames(raw: string): string[] {
  return raw
    .split(/[\s,]+/)
    .map((name) => name.trim())
    .filter((name) => name !== "");
}

/** The same list as the editor shows it back: comma-separated, in the server's order. */
export function formatFieldNames(names: readonly string[]): string {
  return names.join(", ");
}

/**
 * Spanish for one of *our own* request fields.
 *
 * Keyed by the wire names this surface sends, which are structural — they are the schema's
 * fields, not anybody's data — so a validation error can say "el color" instead of echoing
 * `color` at somebody who never saw the JSON.
 */
const FIELD_LABEL: Readonly<Record<string, string>> = {
  code: "el código",
  name: "el nombre",
  applies_to: "a qué se aplica",
  engagement_types: "los tipos de encargo",
  label: "la etiqueta",
  category: "la categoría",
  color: "el color",
  order: "el orden",
  from_state: "el estado de origen",
  to_state: "el estado de destino",
  requires_fields: "los campos requeridos",
  requires_reason: "el motivo obligatorio",
  guard: "la condición (guard)",
  is_active: "el estado del flujo",
};

/**
 * The Spanish noun agreeing with `count`.
 *
 * Every noun this surface counts — estado, movimiento, registro, proyecto, tarea — takes a
 * regular `-s`, so the rule is one line rather than a table. A noun that does not would need
 * its own entry here, which is the point at which this becomes a table.
 */
export function plural(count: number, singular: string): string {
  return count === 1 ? singular : `${singular}s`;
}

/** `2 proyectos`, `1 proyecto` — the count and its noun, together. */
function counted(value: number, singular: string): string {
  return `${value} ${plural(value, singular)}`;
}

/** A `details` value as a string, `""` when the envelope carried none. */
function detail(error: ApiError, key: string): string {
  if (error.kind === "network") return "";
  const value = error.details[key];
  return typeof value === "string" ? value : "";
}

/** A `details` value as a number, `0` when the envelope carried none. */
function detailCount(error: ApiError, key: string): number {
  if (error.kind === "network") return 0;
  const value = error.details[key];
  return typeof value === "number" && Number.isFinite(value) ? value : 0;
}

/**
 * Whether this refusal is "that move already exists, retired or not".
 *
 * The editor offers **restoring** it instead of repeating the request, which is the only way a
 * withdrawn edge comes back: a second row for the same ordered pair is forbidden by constraint,
 * so `POST` can only ever answer 409, and a withdrawn move is absent from every read — without
 * this branch the operator would be told "ya existe" about something they cannot see anywhere.
 */
export function isWithdrawnTransition(error: ApiError): boolean {
  return (
    error.code === "conflicting_state" &&
    detail(error, "entity") === "workflow_transition"
  );
}

/**
 * Spanish for a refused authoring write, naming the rule rather than the status code.
 *
 * Each branch exists because its recovery is different: move the records, restore the row you
 * collided with, unbind the engagement type, or fix the field. Where the server counted
 * something — how many projects and tasks are standing on a state — the number is in the
 * sentence, because "no se pudo retirar" is not something anybody can act on.
 */
export function authoringErrorCopy(error: ApiError): ErrorCopy {
  if (error.kind === "permission_denied") {
    const capability = error.required === "" ? "ops_lead" : error.required;
    return {
      title: "No puedes dar forma a los flujos",
      detail: `Esta acción exige la capacidad «${capability}»: reservada al responsable de operación, porque cambiar un flujo cambia cómo se mueve el trabajo de todo el mundo. Leerlos sigue abierto para cualquiera.`,
    };
  }

  if (error.code === "conflicting_state") {
    return conflictCopy(error);
  }

  if (error.kind === "validation") {
    const fields = Object.keys(error.fields).map(
      (field) => FIELD_LABEL[field] ?? field,
    );
    const messages = Object.values(error.fields).flat();
    if (messages.length > 0 && fields.length === 1) {
      return {
        title: "El servidor rechazó el formulario",
        detail: `Revisa ${fields[0]}: ${messages[0]}`,
      };
    }
    if (fields.length > 0) {
      return {
        title: "El servidor rechazó el formulario",
        detail: `Revisa ${fields.join(", ")}.`,
      };
    }
  }

  if (error.kind === "not_found") {
    return {
      title: "Eso ya no está en el flujo",
      detail:
        "Otra persona pudo retirarlo mientras tenías la pantalla abierta. Vuelve a cargar para ver el flujo como está ahora.",
    };
  }

  return errorCopy(error);
}

/** The four shapes of a 409 on this surface; each one has its own next move. */
function conflictCopy(error: ApiError): ErrorCopy {
  const entity = detail(error, "entity");
  const id = detail(error, "id");
  const current = detail(error, "current");

  if (current === "occupied") {
    const projects = detailCount(error, "projects");
    const tasks = detailCount(error, "tasks");
    const records = detailCount(error, "records") || projects + tasks;
    return {
      title: `«${id}» todavía tiene trabajo encima`,
      detail: `No se puede retirar un estado que alguien ocupa: hay ${counted(records, "registro")} en él (${counted(projects, "proyecto")} y ${counted(tasks, "tarea")}). Muévelos a otro estado y vuelve a intentarlo.`,
    };
  }

  if (entity === "workflow_state") {
    return {
      title: `Este flujo ya tiene el estado «${id}»`,
      detail:
        "El código está ocupado aunque el estado esté retirado: los registros que pasaron por él lo siguen nombrando. Restáuralo desde su fila en lugar de crear otro.",
    };
  }

  if (entity === "workflow_transition") {
    return {
      title: `El movimiento «${id}» ya está declarado`,
      detail:
        "Puede estar retirado, y por eso no aparece en la tabla: un movimiento retirado no es una flecha que se pueda dibujar. Restáuralo en lugar de declarar otro.",
    };
  }

  if (entity === "engagement_type") {
    return {
      title: `«${id}» ya sigue otro flujo`,
      detail: `Ese tipo de encargo está enlazado a «${detail(error, "workflow")}». Un tipo nombra un solo flujo por clase de registro, o la resolución dejaría de ser determinista: quítalo de allí antes de enlazarlo aquí.`,
    };
  }

  return {
    title: `Ya existe un flujo con el código «${id}»`,
    detail:
      "El código es la dirección permanente del flujo, y el que eligiste está ocupado aunque ese flujo esté inactivo. Elige otro.",
  };
}
