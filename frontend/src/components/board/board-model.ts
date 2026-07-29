/**
 * Pure view model of the board surface (`/board`) — no DOM, no Astro, no network.
 *
 * Everything the board renders is derived here from one `GET /api/v1/queue` page, one
 * `GET /api/v1/workflows` and one `GET /api/v1/team/load`, so the page frontmatter composes
 * components instead of computing, and the same functions can be reasoned about (and one day
 * tested) without a browser. The task cards are derived here too, because the island builds
 * them at runtime and the derivation must not fork into a second copy that drifts.
 *
 * The surface is two halves that swap the axes of the old board:
 *
 * - the **rail** is a vertical stack of PROJECT workflow states, one section each, and a
 *   project is dragged up and down between them;
 * - the **board** is a horizontal row of TASK workflow states, one column each, holding the
 *   tasks of the selected project, dragged left and right.
 *
 * Three rules shape this module:
 *
 * - **Data owns colour and order.** A section's tone comes from `StateRef.color` when the
 *   operator set one and falls back to the state's `category`; the order of the sections and
 *   of the columns is the order the operator arranged in the admin, read off
 *   `GET /api/v1/workflows`. Nothing here branches on a state `code`
 *   (`docs/standards/PATTERNS_FRONTEND.md` §7).
 * - **Every state gets a section, occupied or not.** A state with no section is a state you
 *   cannot drop into, which is the defect the workflow endpoint exists to fix.
 * - **Absence is a signal.** A project with no target date or no computed breakdown renders
 *   an ámbar chip naming what is missing, never an empty cell (DESIGN.md, Present-Absence).
 */
import { avatarHue, initials } from "../../lib/auth/session";
import type {
  DependencyRef,
  QueueItem,
  QueueOverride,
  RiskFlag,
  Score,
  StateRef,
  TaskItem,
  TaxonomyRef,
  TeamLoadEntry,
  WorkflowCatalog,
  WorkflowShape,
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

/** One PROJECT workflow state as a section of the rail: the state, its tone, its cards. */
export interface RailSectionModel {
  readonly state: StateRef;
  readonly tone: Tone;
  readonly cards: readonly ProjectCardModel[];
}

/** One TASK workflow state as a column of the main board; its cards arrive on selection. */
export interface TaskColumnModel {
  readonly state: StateRef;
  readonly tone: Tone;
}

/**
 * One engagement type's whole surface: the rail on the left and the task columns on the right.
 *
 * Both halves are bound to the engagement type rather than to the page, because
 * `Workflow.objects.resolve` binds a graph per engagement type: two types can run two
 * different project workflows *and* two different task workflows, so the select swaps the
 * columns as well as the sections.
 */
export interface BoardSurfaceModel {
  readonly engagementType: TaxonomyRef;
  readonly tone: Tone;
  readonly sections: readonly RailSectionModel[];
  readonly taskStates: readonly TaskColumnModel[];
  /** Projects of this type on the rail — the count the select shows beside the label. */
  readonly count: number;
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
  | {
      readonly kind: "absent";
      readonly text: string;
      readonly description: string;
    };

/** A human's forced ranking, as the card labels it. Never merged into the score. */
export interface OverrideNote {
  readonly label: string;
  readonly description: string;
}

/**
 * The risk mark of one card: how many flags the read raised, and which ones.
 *
 * A count rather than a strip of chips, because the rail is 360px wide and six flags would
 * bury the project's name. The full list travels in `description`, so the fact is never lost —
 * only folded. Tone comes from the worst severity present, mapped through the semantic set;
 * a severity the frontend has never seen renders neutral instead of disappearing.
 */
export interface RiskSummary {
  readonly count: number;
  readonly text: string;
  readonly toneClass: string;
  readonly description: string;
}

/**
 * The owner's disk: initials plus the deterministic hue that keeps one person one colour.
 *
 * `alias` travels with it because the board filters by owner, and the filter compares the
 * stable slug the API accepts back — never the display label, which two people can share.
 */
export interface OwnerBadge {
  readonly alias: string;
  readonly label: string;
  readonly initials: string;
  readonly hue: number;
}

/** Everything one project card renders, and nothing else (FRONTEND.md §3, ISP). */
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
  readonly risks: RiskSummary | null;
  /** `updated_at` of the fetch that rendered the card: the stale-patch guard for SSE. */
  readonly updatedAt: string;
}

/**
 * How a person's avatar renders.
 *
 * A discriminated union rather than `photoUrl: string | null` plus initials beside it,
 * because exactly one of the two is ever drawn and the boolean form admits "a photo *and*
 * initials" — a state the markup cannot represent (CLAUDE.md rule 13).
 */
export type MemberAvatar =
  | { readonly kind: "photo"; readonly src: string }
  | { readonly kind: "initials"; readonly text: string; readonly hue: number };

/** One roster member as the board's "Miembros" bar renders and filters by them. */
export interface MemberChipModel {
  readonly alias: string;
  readonly label: string;
  readonly avatar: MemberAvatar;
  readonly openTasks: number;
  /** Full sentence for `title` / `aria-label`; the disk alone names nobody. */
  readonly description: string;
}

/** One prerequisite chip on a task card. */
export interface DependencyChip {
  /** The prerequisite's task code, or — the normal case — the operation's own words. */
  readonly text: string;
  readonly description: string;
  /** True when the reference resolved to a real task rather than staying free text. */
  readonly isResolved: boolean;
}

/** One task as a card on the main board. */
export interface TaskCardModel {
  readonly code: string;
  readonly title: string;
  /** Where the server says it sits; the column the card is placed in. */
  readonly stateCode: string;
  readonly stateLabel: string;
  readonly owner: OwnerBadge | null;
  readonly due: DueChip;
  /** Derived server-side on the server's clock; never recomputed here. */
  readonly isOverdue: boolean;
  readonly dependencies: readonly DependencyChip[];
}

/** Where one project sits on the rail: its surface, its section, and the card itself. */
export interface CardLocation {
  readonly surface: BoardSurfaceModel;
  readonly section: RailSectionModel;
  readonly card: ProjectCardModel;
}

/** Which kind of aggregate a workflow governs, as `WorkflowShapeView.applies_to` spells it. */
const PROJECT_WORKFLOW = "PROJECT";
const TASK_WORKFLOW = "TASK";

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

/**
 * Risk severity → semantic tone.
 *
 * `severity` is a bare code the client branches on to pick a tone (`RiskFlagView`), and this
 * is the only place that branch lives. A severity added server-side renders neutral rather
 * than unstyled — the flag still shows, which is the whole point of the open set.
 */
const SEVERITY_TONE: Record<string, string> = {
  CRITICAL: "tone-rojo",
  HIGH: "tone-ambar",
  MEDIUM: "tone-ambar",
  LOW: "tone-piedra",
};

/** How severe one flag is, for picking the worst of a list. Unknown severities sort last. */
const SEVERITY_RANK: Record<string, number> = {
  CRITICAL: 0,
  HIGH: 1,
  MEDIUM: 2,
  LOW: 3,
};

/** Where a severity the frontend has never seen sorts: after everything it knows. */
const UNKNOWN_SEVERITY_RANK = 9;

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
  title: "título",
  assignee: "responsable",
  due_date: "fecha de entrega",
  last_progress: "último avance",
  priority: "prioridad",
};

/**
 * Resolves the photo of one roster member, or `null` when there is none.
 *
 * The seam exists because `TeamLoadView` and `ActorRef` carry `alias`, `label` and `role` and
 * **no image field at all** (checked against the live OpenAPI document). Initials are
 * therefore the only path that runs today, and the photo arm of {@link MemberAvatar} is dead
 * code on purpose: when the backend grows the field, binding it is one line at the call site
 * in `pages/board.astro` and no component changes shape. Inventing an optional property on
 * the generated type instead would be a hand-written contract the schema never agreed to
 * (`docs/standards/FRONTEND.md` §1).
 */
export type MemberPhotoLookup = (entry: TeamLoadEntry) => string | null;

/** The lookup to pass while the wire carries no image field: every member falls to initials. */
export const NO_MEMBER_PHOTOS: MemberPhotoLookup = () => null;

/**
 * Tone for one workflow state: the operator's own colour when there is one, otherwise the
 * category's semantic tone.
 *
 * Failure mode this prevents: a hardcoded `code → colour` map, which drops a state added from
 * the admin into an unstyled section (`docs/standards/PATTERNS_FRONTEND.md` §7).
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
 * The workflow that governs one engagement type, mirroring `Workflow.objects.resolve`.
 *
 * Precedence, and why: an active graph whose `engagement_types` names this type wins, because
 * that binding is the operator saying "this type runs on that graph"; otherwise the per-kind
 * default; otherwise any graph of the right kind, so a portfolio configured without a default
 * still draws columns instead of nothing. Returns `null` only when the catalog publishes no
 * graph of that kind at all.
 *
 * `is_active` is a preference, not a filter: a retired graph is still published because
 * aggregates keep sitting on its states, and dropping it would lose those projects entirely.
 */
export function resolveWorkflow(
  catalog: WorkflowCatalog,
  appliesTo: string,
  engagementCode: string | null,
): WorkflowShape | null {
  const candidates = catalog.workflows.filter(
    (workflow) => workflow.applies_to === appliesTo,
  );
  if (engagementCode !== null) {
    const bound = candidates.find(
      (workflow) =>
        workflow.is_active &&
        (workflow.engagement_types ?? []).some(
          (type) => type.code === engagementCode,
        ),
    );
    if (bound !== undefined) return bound;
  }
  return (
    candidates.find((workflow) => workflow.is_default && workflow.is_active) ??
    candidates.find((workflow) => workflow.is_default) ??
    candidates.find((workflow) => workflow.is_active) ??
    candidates[0] ??
    null
  );
}

/**
 * Builds one surface per engagement type present in the queue: rail sections on the left,
 * task columns on the right.
 *
 * `catalog` is what makes an unoccupied state renderable, and therefore droppable. When the
 * workflow read failed it is `null` and the sections fall back to the states projects happen
 * to sit in — a degraded board that still works, beside the note the page renders saying so.
 *
 * A project sitting in a state the resolved graph does not publish (a retired graph, a state
 * an operator removed from this workflow) still gets a section, appended after the graph's
 * own: losing the card would be worse than an extra section at the end.
 *
 * Ordering: engagement types by label, sections and columns in the operator's own order,
 * cards in the order the API sent them (score descending), so the top of a section is the
 * most urgent project in it.
 */
export function buildSurfaces(
  items: readonly QueueItem[],
  catalog: WorkflowCatalog | null,
  now: Date,
): BoardSurfaceModel[] {
  const groups = new Map<
    string,
    { type: TaxonomyRef; byState: Map<string, ProjectCardModel[]>; seen: Map<string, StateRef> }
  >();

  for (const item of items) {
    const typeCode = item.engagement_type.code;
    let group = groups.get(typeCode);
    if (group === undefined) {
      group = {
        type: item.engagement_type,
        byState: new Map(),
        seen: new Map(),
      };
      groups.set(typeCode, group);
    }
    group.seen.set(item.state.code, item.state);
    const bucket = group.byState.get(item.state.code);
    if (bucket === undefined) group.byState.set(item.state.code, [toCardModel(item, now)]);
    else bucket.push(toCardModel(item, now));
  }

  return [...groups.values()]
    .map((group) => {
      const projectStates = orderedStates(
        catalog,
        PROJECT_WORKFLOW,
        group.type.code,
        group.seen,
      );
      return {
        engagementType: group.type,
        tone: toneForTaxonomy(group.type),
        sections: projectStates.map((state) => ({
          state,
          tone: toneForState(state),
          cards: group.byState.get(state.code) ?? [],
        })),
        taskStates: orderedStates(
          catalog,
          TASK_WORKFLOW,
          group.type.code,
          new Map(),
        ).map((state) => ({ state, tone: toneForState(state) })),
        count: [...group.byState.values()].reduce(
          (total, cards) => total + cards.length,
          0,
        ),
      };
    })
    .sort((a, b) =>
      a.engagementType.label.localeCompare(b.engagementType.label, "es"),
    );
}

/**
 * The states one surface draws, in the operator's order, with nothing lost.
 *
 * The graph's own states come first, exactly as arranged in the admin; any state the data
 * occupies that the graph does not publish is appended, because a card with no zone is a card
 * that vanishes. With no catalog at all the occupied states are the whole answer.
 */
function orderedStates(
  catalog: WorkflowCatalog | null,
  appliesTo: string,
  engagementCode: string,
  occupied: ReadonlyMap<string, StateRef>,
): StateRef[] {
  const workflow =
    catalog === null ? null : resolveWorkflow(catalog, appliesTo, engagementCode);
  const published = workflow === null ? [] : workflow.states;
  const known = new Set(published.map((state) => state.code));
  const extra = [...occupied.entries()]
    .filter(([code]) => !known.has(code))
    .map(([, state]) => state);
  return [...published, ...extra];
}

/**
 * Locates one project on the built rail, or `null` when no card carries that code.
 *
 * The board's header is rendered from the same card model the section renders, rather than
 * from a second `GET /api/v1/projects/{code}`: the page already holds the row, and a second
 * read of the same fact is a second thing that can disagree with it.
 */
export function findCardLocation(
  surfaces: readonly BoardSurfaceModel[],
  code: string,
): CardLocation | null {
  for (const surface of surfaces) {
    for (const section of surface.sections) {
      for (const card of section.cards) {
        if (card.code === code) return { surface, section, card };
      }
    }
  }
  return null;
}

/**
 * Projects one queue row onto the narrow model the card renders.
 *
 * The card never receives the entity: everything it shows is decided here once, so the
 * component stays a template and the derivations (absence chips, tones, the breakdown
 * sentence) live in one testable place (`docs/standards/FRONTEND.md` §3).
 */
export function toCardModel(item: QueueItem, now: Date): ProjectCardModel {
  return {
    code: item.code,
    name: item.name,
    clientAlias: item.client_alias,
    owner: toOwnerBadge(item.owner ?? null),
    stateCode: item.state.code,
    stateLabel: item.state.label,
    due: formatDue(item.target_date ?? null, now),
    score: describeScore(item.score),
    manualOverride: describeOverride(item.override ?? null),
    risks: summariseRisks(item.risk_flags),
    updatedAt: item.updated_at,
  };
}

/** The owner's disk, or `null` when nobody owns the row — an absence the card names. */
export function toOwnerBadge(
  actor: { alias: string; label: string } | null,
): OwnerBadge | null {
  if (actor === null) return null;
  return {
    alias: actor.alias,
    label: actor.label,
    initials: initials(actor.label),
    hue: avatarHue(actor.alias),
  };
}

/**
 * Folds a row's risk flags into one chip: the count, the worst severity's tone, and every
 * label in the accessible description.
 *
 * Returns `null` for a clean project rather than a "0 riesgos" chip: the absence of risk is
 * the default and does not need a mark, while the presence of it does.
 */
export function summariseRisks(flags: readonly RiskFlag[]): RiskSummary | null {
  if (flags.length === 0) return null;
  const worst = flags.reduce((current, flag) =>
    severityRank(flag.severity) < severityRank(current.severity) ? flag : current,
  );
  return {
    count: flags.length,
    text: flags.length === 1 ? "1 riesgo" : `${flags.length} riesgos`,
    toneClass: SEVERITY_TONE[worst.severity] ?? NEUTRAL_TONE,
    description: `Riesgos: ${flags.map((flag) => flag.label).join(" · ")}`,
  };
}

function severityRank(severity: string): number {
  return SEVERITY_RANK[severity] ?? UNKNOWN_SEVERITY_RANK;
}

/**
 * Builds the "Miembros" bar from the roster's load.
 *
 * Order is the server's — `GET /api/v1/team/load` is the whole roster, arranged as the
 * operation arranged it — because re-sorting a five-person bar on every render makes the same
 * face land in a different place each morning, which is exactly what an avatar row must not do.
 * A member with no open work still appears: the filter has to be able to answer "nothing".
 */
export function buildMembers(
  entries: readonly TeamLoadEntry[],
  photoOf: MemberPhotoLookup = NO_MEMBER_PHOTOS,
): MemberChipModel[] {
  return entries.map((entry) => {
    const photo = photoOf(entry);
    const avatar: MemberAvatar =
      photo === null || photo === ""
        ? {
            kind: "initials",
            text: initials(entry.label),
            hue: avatarHue(entry.alias),
          }
        : { kind: "photo", src: photo };
    return {
      alias: entry.alias,
      label: entry.label,
      avatar,
      openTasks: entry.open_tasks,
      description: `${entry.label} · ${describeOpenTasks(entry.open_tasks)}`,
    };
  });
}

function describeOpenTasks(count: number): string {
  if (count === 0) return "sin tareas abiertas";
  return count === 1 ? "1 tarea abierta" : `${count} tareas abiertas`;
}

/** Projects one task onto the card the main board renders. */
export function toTaskCard(task: TaskItem, now: Date): TaskCardModel {
  return {
    code: task.code,
    title: task.title,
    stateCode: task.state.code,
    stateLabel: task.state.label,
    owner: toOwnerBadge(task.assignee ?? null),
    due: formatDue(task.due_date ?? null, now),
    isOverdue: task.is_overdue,
    dependencies: task.dependencies.map(toDependencyChip),
  };
}

/**
 * One dependency as a chip.
 *
 * `task_code: null` with a `raw_label` is the normal case — most source tasks name their
 * prerequisite in prose — so the chip renders the words somebody wrote rather than hiding an
 * unresolved reference behind a dash.
 */
export function toDependencyChip(dependency: DependencyRef): DependencyChip {
  const code = dependency.task_code ?? null;
  const raw = dependency.raw_label;
  if (code !== null && code !== "") {
    return {
      text: code,
      description: raw === "" ? `Depende de ${code}` : `Depende de ${raw}`,
      isResolved: true,
    };
  }
  const text = raw === "" ? "sin detalle" : raw;
  return {
    text,
    description: `Depende de: ${text}`,
    isResolved: false,
  };
}

/**
 * Formats a target date as a due chip.
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
export function describeOverride(
  override: QueueOverride | null,
): OverrideNote | null {
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
