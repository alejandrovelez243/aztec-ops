/**
 * Domain aliases for the Aztec Ops wire, derived from the generated OpenAPI tree.
 *
 * Nothing here is hand-written: every alias resolves to `components["schemas"]` (or to
 * an operation's query parameters) in `./types.ts`, which `npm run gen:api` regenerates
 * from the live backend. A field that is missing is a backend schema change to request,
 * not a local `interface` to add (`docs/standards/FRONTEND.md` §1).
 */
import type { components, operations } from "./types";

type Schemas = components["schemas"];

/** The `parameters.query` bag of one generated operation, with the optionality kept. */
type QueryOf<K extends keyof operations> = NonNullable<
  operations[K]["parameters"]["query"]
>;

// --- Shared references -------------------------------------------------------

/** One person as every payload renders them; `alias` is the slug the API accepts back. */
export type ActorRef = Schemas["ActorRef"];

/** One operator-editable catalog value: render `label`/`color`, compare only `code`. */
export type TaxonomyRef = Schemas["TaxonomyRef"];

/** One workflow state; branch on `category`, never on a list of `code`s. */
export type StateRef = Schemas["StateRef"];

/** Derived project health; structural, never writable. */
export type HealthRef = Schemas["HealthRef"];

// --- Queue -------------------------------------------------------------------

/** One row of the prioritized queue, read from the snapshot read model. */
export type QueueItem = Schemas["QueueItemView"];

/** One page of the queue: `count` is the total across pages, not the page length. */
export type QueuePage = Schemas["Page_QueueItemView_"];

/** The queue row's manual-override fact; narrower than the detail's `Override`. */
export type QueueOverride = Schemas["QueueOverrideView"];

// --- Scores and risk ---------------------------------------------------------

/** A computed 0–100 rank with the breakdown that defends it; overrides never rewrite it. */
export type Score = Schemas["ScoreView"];

/** One signal line: what it read, what it weighed, why. Order is the server's. */
export type ScoreBreakdownEntry = Schemas["ScoreSignalView"];

/** One raised risk; `code` is an open set, render unknown codes with their `reason`. */
export type RiskFlag = Schemas["RiskFlagView"];

/** A human's forced ranking decision, with its mandatory `reason`. */
export type Override = Schemas["OverrideView"];

/** Response of the override endpoint: computed score and the decision beside it. */
export type OverrideResult = Schemas["OverrideOut"];

// --- Project detail ----------------------------------------------------------

/** One project in full; `transitions` is the only source of transition buttons. */
export type ProjectDetail = Schemas["ProjectDetailView"];

/** One legal move out of the current state; `requires_fields` names empty attributes. */
export type Transition = Schemas["TransitionOption"];

// --- Tasks -------------------------------------------------------------------

/** One task; `is_overdue` is derived server-side, never a stored flag. */
export type TaskItem = Schemas["TaskView"];

/** One page of a project's tasks. */
export type TaskPage = Schemas["Page_TaskView_"];

/** One task prerequisite; `task_code: null` with a `raw_label` is the normal case. */
export type DependencyRef = Schemas["DependencyRef"];

// --- Blockers and notes ------------------------------------------------------

/** One impediment; `resolved_at: null` *is* the definition of open. */
export type Blocker = Schemas["BlockerView"];

/** The closed set of blocker kinds; a new member is a migration, not a fixture row. */
export type BlockerKind = Schemas["BlockerKind"];

/** One chronological comment; `author` is a bare code that can outlive its user row. */
export type NoteView = Schemas["NoteView"];

// --- Team load ---------------------------------------------------------------

/** One person's current load, computed from task rows at read time. */
export type TeamLoadEntry = Schemas["TeamLoadView"];

/** The whole roster's load; not paginated and deliberately without a `count`. */
export type TeamLoad = Schemas["TeamLoadPage"];

// --- Activity ----------------------------------------------------------------

/** One persisted audit-trail fact; `metadata` is free-form per verb. */
export type ActivityEntry = Schemas["ActivityEntry"];

/** One page of the timeline, always newest first. */
export type ActivityPage = Schemas["Page_ActivityEntry_"];

// --- Request bodies ----------------------------------------------------------

export type ProjectTransitionIn = Schemas["TransitionIn"];
export type TaskTransitionIn = Schemas["TaskTransitionIn"];
export type PriorityOverrideIn = Schemas["OverrideIn"];
export type BlockerCreateIn = Schemas["BlockerCreateIn"];
export type BlockerResolveIn = Schemas["BlockerResolveIn"];
export type NoteIn = Schemas["NoteIn"];
export type TaskCreateIn = Schemas["TaskCreateIn"];

// --- Query parameters --------------------------------------------------------

/**
 * Input bags for the list endpoints, taken from the operations rather than the
 * `*Query` schemas: the schemas carry ninja's server-side defaults as required
 * fields, while on the wire every parameter is optional.
 */
export type QueueQuery = QueryOf<"apps_portfolio_api_routers_get_queue">;
export type TaskQuery = QueryOf<"apps_work_api_routers_get_project_tasks">;
export type TeamLoadQuery = QueryOf<"apps_portfolio_api_routers_get_team_load">;
export type TimelineQuery =
  QueryOf<"apps_activity_api_routers_get_project_activity">;
