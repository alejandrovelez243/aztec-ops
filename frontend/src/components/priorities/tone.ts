/**
 * Semantic tone selection for the priorities surface.
 *
 * The single rule this module encodes: colour comes from a risk flag's `severity`
 * or from a taxonomy's own `color`, **never** from its `code`
 * (`docs/standards/PATTERNS_FRONTEND.md` §7). A specification added on the backend
 * ships a code this file has never seen and still renders — with its severity's tone
 * when the severity is known, and with the neutral tone when it is not. A flag is
 * never dropped: a dropped flag is a risk nobody sees.
 */

/** The five tone classes `styles/base.css` defines; nothing else may colour data. */
export type ToneClass =
  "tone-rojo" | "tone-ambar" | "tone-verde" | "tone-cielo" | "tone-piedra";

/**
 * Severity vocabulary → tone (`docs/API.md` §2.2, DESIGN.md §Colors).
 *
 * `CRITICAL` and `HIGH` share Rojo because DESIGN.md assigns Rojo to "blocked,
 * overdue, critical risk" and `OVERDUE` is a `HIGH` flag; the two are told apart by
 * the halo {@link isTopSeverity} drives, not by a second red. Keyed by severity and
 * not by flag code, which is what keeps this open to new specifications.
 */
const SEVERITY_TONE: Readonly<Record<string, ToneClass>> = {
  CRITICAL: "tone-rojo",
  HIGH: "tone-rojo",
  MEDIUM: "tone-ambar",
  LOW: "tone-verde",
};

/** Order the strip renders severities in; unknown severities sort last. */
const SEVERITY_RANK: Readonly<Record<string, number>> = {
  CRITICAL: 0,
  HIGH: 1,
  MEDIUM: 2,
  LOW: 3,
};

/** The severity that earns the halo ring, so it is distinguishable from `HIGH`. */
const TOP_SEVERITY = "CRITICAL";

/** Rank given to a severity this frontend does not know; keeps it visible, last. */
const UNKNOWN_SEVERITY_RANK = 90;

/**
 * The tone class for one risk severity.
 *
 * An unrecognised severity returns the neutral tone rather than throwing or
 * defaulting to red: inventing an alarm for a value we do not understand is worse
 * than rendering it plainly beside its reason.
 */
export function toneForSeverity(severity: string): ToneClass {
  return SEVERITY_TONE[severity] ?? "tone-piedra";
}

/** Whether this severity is the highest the API defines — the halo case. */
export function isTopSeverity(severity: string): boolean {
  return severity === TOP_SEVERITY;
}

/**
 * Sort key for a severity, worst first.
 *
 * Presentation only: the API sends flags in registration order, and this reorders
 * them so the strip reads worst-to-least without changing which flags exist.
 */
export function severityRank(severity: string): number {
  return SEVERITY_RANK[severity] ?? UNKNOWN_SEVERITY_RANK;
}

/**
 * How to paint one operator-editable taxonomy value (a workflow state, a client).
 *
 * A taxonomy that carries a `color` renders through `.tone-data`, which derives an
 * AA-readable ink and a wash from the solid the admin chose — so recolouring a state
 * in the admin needs no frontend change. A taxonomy with no colour falls back to the
 * neutral tone instead of painting a swatch of transparent black (`TaxonomyRef`
 * documents `null` as "the operator set none").
 */
export interface TaxonomyTone {
  /** Class list to place on the element. */
  readonly className: string;
  /** Inline `--tone-solid`, or `undefined` when the fallback tone is used. */
  readonly style: string | undefined;
}

/** Builds the {@link TaxonomyTone} for one taxonomy colour. */
export function toneForColor(color: string | null | undefined): TaxonomyTone {
  if (color === null || color === undefined || color === "") {
    return { className: "tone-piedra", style: undefined };
  }
  return { className: "tone-data", style: `--tone-solid: ${color}` };
}
