/**
 * Tone selection for the projects surfaces — the frontend half of the
 * Data-Owns-Color Rule (DESIGN.md §Colors).
 *
 * Nothing here is keyed by a *business* code. A workflow state added from the
 * admin arrives with its own `color` and renders through `.tone-data`; when the
 * operator set none, the fallback is chosen from `category`, which is the only
 * comparison the frontend is allowed to make on a state
 * (`docs/standards/PATTERNS_FRONTEND.md` §7). Risk flags are keyed by
 * `severity`, never by `code`, because the flag code set is open and a code this
 * build does not know must still render.
 *
 * Failure mode this prevents: a map keyed by `state.code` silently drops the
 * state somebody added this morning, and the chip renders unstyled or empty.
 */

/** The five semantic tone classes declared in `styles/base.css`. */
export type ToneClass =
  "tone-rojo" | "tone-ambar" | "tone-verde" | "tone-cielo" | "tone-piedra";

/**
 * What a chip needs to paint itself: the tone class, plus the solid colour when
 * it came from the API rather than from the semantic set.
 *
 * `solid` is `null` rather than optional so the same value can be handed to an
 * Astro attribute and to `element.style` without tripping
 * `exactOptionalPropertyTypes`; Astro omits an attribute whose value is `null`.
 */
export interface ToneSpec {
  readonly className: string;
  readonly solid: string | null;
}

/**
 * Colours the admin may configure, conservatively bounded.
 *
 * The value lands in an inline `style` attribute, so anything able to close the
 * declaration could smuggle a second one in. Hex, `rgb()`/`hsl()` and bare
 * colour keywords all pass; a value carrying `;`, quotes or braces does not and
 * falls back to the semantic tone — an unstyled chip beats an injected one.
 */
const SAFE_COLOR = /^#?[A-Za-z0-9(),.%\s/-]{1,64}$/;

/** Tone of last resort: neutral, never mistaken for a semantic verdict. */
const NEUTRAL: ToneClass = "tone-piedra";

/**
 * Workflow-state categories are a closed, structural vocabulary (DATA_MODEL
 * §12: adding one is a migration, not a fixture row), so mapping them is safe —
 * unlike mapping state codes, which the operator owns.
 */
const CATEGORY_TONE: Readonly<Record<string, ToneClass>> = {
  BACKLOG: "tone-piedra",
  IN_PROGRESS: "tone-cielo",
  BLOCKED: "tone-rojo",
  DONE: "tone-verde",
  CANCELLED: "tone-piedra",
};

/** Spanish names of the same closed set; the raw code is the fallback. */
const CATEGORY_LABEL: Readonly<Record<string, string>> = {
  BACKLOG: "Pendiente",
  IN_PROGRESS: "En progreso",
  BLOCKED: "Bloqueado",
  DONE: "Terminado",
  CANCELLED: "Cancelado",
};

/**
 * Risk severities are the four values of `Severity` (closed set, backend
 * `apps/prioritization/domain/types.py`). Rojo covers what stops work, ámbar
 * what warns, per DESIGN.md §Colors.
 */
const SEVERITY_TONE: Readonly<Record<string, ToneClass>> = {
  CRITICAL: "tone-rojo",
  HIGH: "tone-rojo",
  MEDIUM: "tone-ambar",
  LOW: "tone-cielo",
};

/** Derived health is a pure function of the flags; three values, closed. */
const HEALTH_TONE: Readonly<Record<string, ToneClass>> = {
  HEALTHY: "tone-verde",
  AT_RISK: "tone-ambar",
  BLOCKED: "tone-rojo",
};

/** Tone for a workflow-state category; an unknown category stays neutral. */
function categoryTone(category: string): ToneClass {
  return CATEGORY_TONE[category] ?? NEUTRAL;
}

/**
 * Spanish label of a workflow-state category, for the filter pills that group
 * states. An unrecognised category renders its own code rather than vanishing.
 */
export function categoryLabel(category: string): string {
  return CATEGORY_LABEL[category] ?? category;
}

/** Tone for a risk flag's severity; an unknown severity stays neutral. */
export function severityTone(severity: string): ToneClass {
  return SEVERITY_TONE[severity] ?? NEUTRAL;
}

/** Tone for derived project health; an unknown code stays neutral. */
export function healthTone(code: string): ToneClass {
  return HEALTH_TONE[code] ?? NEUTRAL;
}

/**
 * Tone for a taxonomy value that carries its own colour (state, engagement
 * type, client, priority).
 *
 * With a colour: `.tone-data` plus the inline `--tone-solid` it derives ink and
 * wash from, so any colour configured in the admin renders with AA-legible text
 * and zero frontend changes. Without one: `fallback`, because a swatch of
 * transparent black is worse than an honest neutral.
 */
export function taxonomyTone(
  color: string | null | undefined,
  fallback: ToneClass = NEUTRAL,
): ToneSpec {
  if (color === null || color === undefined || color === "") {
    return { className: fallback, solid: null };
  }
  if (!SAFE_COLOR.test(color)) {
    return { className: fallback, solid: null };
  }
  return { className: "tone-data", solid: color };
}

/**
 * The inline style a tone needs, or `null` when the tone class carries its own
 * colours. Astro omits an attribute whose value is `null`, so this is safe to
 * bind directly.
 */
export function toneStyle(tone: ToneSpec): string | null {
  return tone.solid === null ? null : `--tone-solid: ${tone.solid}`;
}

/** A tone that needs no inline colour, for the semantic set. */
export function semanticTone(className: ToneClass): ToneSpec {
  return { className, solid: null };
}

/**
 * Tone for one workflow state: its own colour when the operator set one, the
 * category's semantic tone otherwise.
 */
export function stateTone(state: {
  readonly category: string;
  readonly color?: string | null;
}): ToneSpec {
  return taxonomyTone(state.color, categoryTone(state.category));
}
