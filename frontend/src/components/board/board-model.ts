/**
 * Pure view model of the board surface (`/board`) — no DOM, no Astro, no network.
 *
 * Everything the board renders is derived here from one `GET /api/v1/queue` page, so the
 * page frontmatter composes components instead of computing, and the same functions can be
 * reasoned about (and one day tested) without a browser.
 *
 * Two rules shape this module:
 *
 * - **Data owns colour and order.** A column's tone comes from `StateRef.color` when the
 *   operator set one and falls back to the state's `category`; nothing here branches on a
 *   state `code` (`docs/standards/PATTERNS_FRONTEND.md` §7). A workflow state added from the
 *   admin therefore renders with zero frontend changes.
 * - **Absence is a signal.** A project with no target date or no computed breakdown renders
 *   an ámbar chip naming what is missing, never an empty cell (DESIGN.md, Present-Absence).
 */
import { avatarHue, initials } from "../../lib/auth/session";
import type {
  QueueItem,
  QueueOverride,
  Score,
  StateRef,
  TaxonomyRef,
} from "../../lib/api/domain";

/**
 * A tone binding for one element: the class list to apply plus the inline custom property
 * that feeds it, or `null` when the tone is a static class from `base.css`.
 *
 * The inline half exists because `.tone-data` derives its ink and wash from `--tone-solid`
 * with `color-mix()`: a colour the operator picks in the admin arrives on the wire and lands
 * on the element without a single literal in this codebase.
 */
export interface Tone {
  readonly className: string;
  readonly style: string | null;
}

/** One column of the board: a workflow state plus the cards currently sitting in it. */
export interface BoardColumnModel {
  readonly state: StateRef;
  readonly tone: Tone;
  readonly cards: readonly ProjectCardModel[];
}

/** One engagement type's board — the columns the switcher shows one at a time. */
export interface BoardPanelModel {
  readonly engagementType: TaxonomyRef;
  readonly tone: Tone;
  readonly columns: readonly BoardColumnModel[];
}

/** The due-date chip: a countdown, or the named absence of a date. */
export interface DueChip {
  /** Short readout for the chip itself: `3 d`, `hoy`, `+2 d`, `sin fecha`. */
  readonly text: string;
  readonly toneClass: string;
  /** Full sentence for `title` and `aria-label`; the chip text alone is an abbreviation. */
  readonly description: string;
  /** True when the chip names a missing date rather than a distance to one. */
  readonly isAbsence: boolean;
}

/**
 * How one card renders its priority.
 *
 * A discriminated union rather than a nullable number, because "scored 0" and "never scored"
 * are different facts and a card that prints `0` for the second one lies about the queue.
 * `computed` always carries its breakdown in `description`: a score without the argument that
 * defends it is forbidden (CLAUDE.md rule 7).
 */
export type ScoreDisplay =
  | {
      readonly kind: "computed";
      readonly value: number;
      readonly text: string;
      readonly description: string;
    }
  | { readonly kind: "absent"; readonly text: string; readonly description: string };

/** A human's forced ranking, as the card labels it. Never merged into the score. */
export interface OverrideNote {
  readonly label: string;
  readonly description: string;
}

/** The owner's disk: initials plus the deterministic hue that keeps one person one colour. */
export interface OwnerBadge {
  readonly label: string;
  readonly initials: string;
  readonly hue: number;
}

/** Everything one project mini-card renders, and nothing else (FRONTEND.md §3, ISP). */
export interface ProjectCardModel {
  readonly code: string;
  readonly name: string;
  readonly clientAlias: string;
  readonly owner: OwnerBadge | null;
  readonly stateCode: string;
  readonly stateLabel: string;
  readonly due: DueChip;
  readonly score: ScoreDisplay;
  readonly manualOverride: OverrideNote | null;
  readonly openBlockers: number;
  /** `updated_at` of the fetch that rendered the card: the stale-patch guard for SSE. */
  readonly updatedAt: string;
}

/**
 * Column order on the wall: what is waiting, what is moving, what is stuck, what is done.
 *
 * Keyed by `StateRef.category` — the closed structural set (`docs/DATA_MODEL.md` §12) — and
 * never by `code`, which is unique only inside its own workflow. An unlisted category sorts
 * last instead of disappearing.
 */
const CATEGORY_ORDER: Record<string, number> = {
  BACKLOG: 0,
  IN_PROGRESS: 1,
  BLOCKED: 2,
  DONE: 3,
  CANCELLED: 4,
};

/** Where a category the frontend has never seen sorts: after everything it knows. */
const UNKNOWN_CATEGORY_RANK = 9;

/**
 * Category → semantic tone, exactly as DESIGN.md §Colors assigns them. Used only when the
 * taxonomy carries no colour of its own, so the wall still reads as a wall.
 */
const CATEGORY_TONE: Record<string, string> = {
  BACKLOG: "tone-piedra",
  IN_PROGRESS: "tone-cielo",
  BLOCKED: "tone-rojo",
  DONE: "tone-verde",
  CANCELLED: "tone-piedra",
};

/** Neutral tone for a category (or a taxonomy) the frontend cannot place. */
const NEUTRAL_TONE = "tone-piedra";

/** Days from today inside which a deadline stops being neutral and starts warning. */
const FINAL_WEEK_DAYS = 7;

const DAY_MS = 86_400_000;

/**
 * Spanish names of the aggregate attributes a transition can require.
 *
 * `requires_fields` names attributes of the project, not of the request body, and the board
 * has to say *which* one is missing rather than let the operator discover it through a 422
 * (`docs/standards/FRONTEND.md` §8). An attribute that is not listed renders under its own
 * wire name: an unexplained field is still better than a silent refusal.
 */
const FIELD_LABELS: Record<string, string> = {
  name: "nombre",
  summary: "resumen",
  next_step: "próximo paso",
  target_date: "fecha objetivo",
  start_date: "fecha de inicio",
  business_value: "valor de negocio",
  owner: "responsable",
  stage: "etapa",
  project_type: "tipo de proyecto",
};

/**
 * Sort rank of one workflow category. Unknown categories collapse onto
 * {@link UNKNOWN_CATEGORY_RANK} so a category added server-side lands at the end of the
 * board instead of reordering it arbitrarily.
 */
export function categoryRank(category: string): number {
  return CATEGORY_ORDER[category] ?? UNKNOWN_CATEGORY_RANK;
}

/**
 * Tone for one workflow state: the operator's own colour when there is one, otherwise the
 * category's semantic tone.
 *
 * Failure mode this prevents: a hardcoded `code → colour` map, which drops a state added from
 * the admin into an unstyled column (`docs/standards/PATTERNS_FRONTEND.md` §7).
 */
export function toneForState(state: StateRef): Tone {
  return toneFromColor(state.color ?? null, state.category);
}

/** Tone for one taxonomy value (engagement type, client, priority) under the same rule. */
export function toneForTaxonomy(taxonomy: TaxonomyRef): Tone {
  return toneFromColor(taxonomy.color ?? null, null);
}

function toneFromColor(color: string | null, category: string | null): Tone {
  if (color !== null && color !== "") {
    return { className: "tone-data", style: `--tone-solid: ${color}` };
  }
  if (category === null) return { className: NEUTRAL_TONE, style: null };
  return { className: CATEGORY_TONE[category] ?? NEUTRAL_TONE, style: null };
}

/**
 * Groups one queue page into the boards the switcher toggles between.
 *
 * Both levels are derived from the rows themselves: one panel per engagement type present,
 * one column per workflow state present inside it. That is deliberately *not* the full set of
 * states — the queue cannot report a state nobody is in — which is why an empty column is
 * missing rather than empty (see the page's `unresolved` note).
 *
 * Ordering: panels by engagement label, columns by category then label, cards in the order
 * the API sent them (score descending), so the top of a column is the most urgent card in it.
 */
export function buildPanels(
  items: readonly QueueItem[],
  now: Date,
): BoardPanelModel[] {
  const panels = new Map<
    string,
    { type: TaxonomyRef; columns: Map<string, { state: StateRef; cards: ProjectCardModel[] }> }
  >();

  for (const item of items) {
    const typeCode = item.engagement_type.code;
    let panel = panels.get(typeCode);
    if (panel === undefined) {
      panel = { type: item.engagement_type, columns: new Map() };
      panels.set(typeCode, panel);
    }
    let column = panel.columns.get(item.state.code);
    if (column === undefined) {
      column = { state: item.state, cards: [] };
      panel.columns.set(item.state.code, column);
    }
    column.cards.push(toCardModel(item, now));
  }

  return [...panels.values()]
    .map((panel) => ({
      engagementType: panel.type,
      tone: toneForTaxonomy(panel.type),
      columns: [...panel.columns.values()]
        .sort(compareColumns)
        .map((column) => ({
          state: column.state,
          tone: toneForState(column.state),
          cards: column.cards,
        })),
    }))
    .sort((a, b) =>
      a.engagementType.label.localeCompare(b.engagementType.label, "es"),
    );
}

function compareColumns(
  a: { state: StateRef },
  b: { state: StateRef },
): number {
  const rank = categoryRank(a.state.category) - categoryRank(b.state.category);
  if (rank !== 0) return rank;
  return a.state.label.localeCompare(b.state.label, "es");
}

/**
 * Projects one queue row onto the narrow model the mini-card renders.
 *
 * The card never receives the entity: everything it shows is decided here once, so the
 * component stays a template and the derivations (absence chips, tones, the breakdown
 * sentence) live in one testable place (`docs/standards/FRONTEND.md` §3).
 */
export function toCardModel(item: QueueItem, now: Date): ProjectCardModel {
  const owner = item.owner ?? null;
  return {
    code: item.code,
    name: item.name,
    clientAlias: item.client_alias,
    owner:
      owner === null
        ? null
        : {
            label: owner.label,
            initials: initials(owner.label),
            hue: avatarHue(owner.alias),
          },
    stateCode: item.state.code,
    stateLabel: item.state.label,
    due: formatDue(item.target_date ?? null, now),
    score: describeScore(item.score),
    manualOverride: describeOverride(item.override ?? null),
    openBlockers: item.open_blockers,
    updatedAt: item.updated_at,
  };
}

/**
 * Formats a target date as the card's due chip.
 *
 * A missing date is the `NO_TARGET_DATE` signal itself, so it renders as the ámbar words
 * "sin fecha" — substituting today's date, or an empty cell, erases a real risk
 * (DESIGN.md, The Present-Absence Rule).
 *
 * Distance is measured in whole calendar days on the UTC clock, because the API sends
 * `YYYY-MM-DD` (which parses as UTC midnight); doing it in local time shifts the readout by
 * one across a DST boundary.
 */
export function formatDue(targetDate: string | null, now: Date): DueChip {
  if (targetDate === null || targetDate === "") {
    return {
      text: "sin fecha",
      toneClass: "tone-ambar",
      description: "Sin fecha objetivo comprometida",
      isAbsence: true,
    };
  }
  const days = calendarDayDiff(new Date(targetDate), now);
  const readable = formatSpanishDate(targetDate);
  if (days < 0) {
    const late = Math.abs(days);
    return {
      text: `+${late} d`,
      toneClass: "tone-rojo",
      description: `Venció el ${readable}, hace ${late} ${plural(late, "día", "días")}`,
      isAbsence: false,
    };
  }
  if (days === 0) {
    return {
      text: "hoy",
      toneClass: "tone-ambar",
      description: `Vence hoy, ${readable}`,
      isAbsence: false,
    };
  }
  return {
    text: `${days} d`,
    toneClass: days <= FINAL_WEEK_DAYS ? "tone-ambar" : "tone-piedra",
    description: `Vence el ${readable}, en ${days} ${plural(days, "día", "días")}`,
    isAbsence: false,
  };
}

/**
 * Describes the computed score, breakdown included.
 *
 * An empty `breakdown` means the engine has not argued for this row yet, and CLAUDE.md rule 7
 * forbids showing a number without its argument — so the card names the absence instead of
 * printing a figure nobody can defend. The signal codes travel verbatim inside a Spanish
 * frame: they are registry data (a new signal appears here for free), and the full persisted
 * reasons live one navigation away on `/priorities`.
 */
export function describeScore(score: Score): ScoreDisplay {
  const entries = score.breakdown;
  if (entries.length === 0) {
    return {
      kind: "absent",
      text: "sin puntaje",
      description: "Todavía sin prioridad calculada",
    };
  }
  const lines = entries
    .map((entry) => `${entry.code} ${formatSigned(entry.contribution)}`)
    .join(" · ");
  return {
    kind: "computed",
    value: score.value,
    text: String(Math.round(score.value)),
    description: `Prioridad ${Math.round(score.value)} de 100 · política ${score.policy_version} · ${lines}`,
  };
}

/**
 * Describes a manual override so the card can label it as a human decision.
 *
 * The computed score is never rewritten by it: both render, because the disagreement between
 * the engine and the person who argued with it is the only evidence that the argument
 * happened (`docs/API.md`, `QueueItemView`).
 */
export function describeOverride(override: QueueOverride | null): OverrideNote | null {
  if (override === null) return null;
  const position = override.position ?? null;
  const place = position === null ? "" : ` a la posición ${position}`;
  return {
    label: "Ajuste manual",
    description: `Prioridad forzada${place}: ${override.reason}`,
  };
}

/**
 * Spanish name of one aggregate attribute named by `requires_fields`, falling back to the
 * wire name so an attribute added server-side is still named rather than swallowed.
 */
export function fieldLabel(field: string): string {
  return FIELD_LABELS[field] ?? field;
}

/** Joins field names into a readable Spanish enumeration: "próximo paso y fecha objetivo". */
export function fieldList(fields: readonly string[]): string {
  const labels = fields.map(fieldLabel);
  if (labels.length <= 1) return labels[0] ?? "";
  return `${labels.slice(0, -1).join(", ")} y ${labels[labels.length - 1] ?? ""}`;
}

function formatSigned(value: number): string {
  const rounded = Math.round(value);
  return rounded >= 0 ? `+${rounded}` : String(rounded);
}

function plural(count: number, one: string, many: string): string {
  return count === 1 ? one : many;
}

/** `2026-08-14` → `14 ago 2026`, on the UTC clock the API dates are expressed in. */
function formatSpanishDate(isoDate: string): string {
  return new Intl.DateTimeFormat("es", {
    day: "numeric",
    month: "short",
    year: "numeric",
    timeZone: "UTC",
  }).format(new Date(isoDate));
}

function calendarDayDiff(to: Date, from: Date): number {
  const toMidnight = Date.UTC(
    to.getUTCFullYear(),
    to.getUTCMonth(),
    to.getUTCDate(),
  );
  const fromMidnight = Date.UTC(
    from.getUTCFullYear(),
    from.getUTCMonth(),
    from.getUTCDate(),
  );
  return Math.round((toMidnight - fromMidnight) / DAY_MS);
}
