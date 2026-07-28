/**
 * Typed client for the Aztec Ops REST API — the only module outside the stream store
 * that may call `fetch` (`docs/standards/PATTERNS_FRONTEND.md` §3).
 *
 * Every function returns a `Result` and never throws: domain rejections arrive as the
 * typed envelope (`docs/API.md` §1.5) and are mapped onto `ApiError` in `./errors.ts`,
 * network failures become `kind: "network"`, and a body that is not JSON becomes
 * `kind: "unknown"` rather than a `SyntaxError` in a component.
 */
import { errorFromNetwork, errorFromResponse, type ApiError } from "./errors";
import type {
  ActivityPage,
  Blocker,
  BlockerCreateIn,
  BlockerResolveIn,
  NoteIn,
  NoteView,
  OverrideResult,
  PriorityOverrideIn,
  ProjectDetail,
  ProjectTransitionIn,
  QueuePage,
  QueueQuery,
  TaskCreateIn,
  TaskItem,
  TaskPage,
  TaskQuery,
  TaskTransitionIn,
  TeamLoad,
  TeamLoadQuery,
  TimelineQuery,
} from "./domain";

/**
 * The outcome of one request: either the typed payload or the typed failure.
 *
 * Callers narrow with `if (!result.ok)`; there is no third "threw" path to handle.
 */
export type Result<T> = { ok: true; data: T } | { ok: false; error: ApiError };

/** Query-string values the API accepts; repeatable filters arrive as lists and OR. */
type QueryValue =
  string | number | boolean | readonly string[] | null | undefined;

const BASE_URL: string =
  import.meta.env.INTERNAL_API_URL ??
  import.meta.env.PUBLIC_API_URL ??
  "http://localhost:8000";

/**
 * The actor sent as `X-Actor` on every request.
 *
 * There is no real authentication in this scope (`docs/API.md` §1.2), so the actor is a
 * module-level setting: the default matches the local seed roster, and an actor picker
 * — or a deployment — overrides it through `setActor` or `PUBLIC_ACTOR`. A value that
 * names no `accounts.User.code` is answered `422 validation_error` with
 * `details.fields["X-Actor"]`, which surfaces here as a `validation` error rather than
 * being repaired silently.
 */
let actor: string = import.meta.env.PUBLIC_ACTOR ?? "camila.torres";

/**
 * Sets the actor slug sent as `X-Actor` from the next request on.
 *
 * The value must be an `accounts.User.code` slug; `system` is rejected from HTTP
 * clients and reserved to consumers (`docs/API.md` §1.2).
 */
export function setActor(next: string): void {
  actor = next;
}

/** Returns the actor slug currently sent as `X-Actor`. */
export function getActor(): string {
  return actor;
}

/**
 * Serializes a query bag: `undefined` and `null` are omitted, lists repeat the key
 * (which is what a multi-select means, `docs/API.md` §1.4), booleans render lowercase.
 */
function toQueryString(query: Record<string, QueryValue>): string {
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(query)) {
    if (value === undefined || value === null) continue;
    if (Array.isArray(value)) {
      for (const item of value) params.append(key, item);
      continue;
    }
    params.append(key, String(value));
  }
  const rendered = params.toString();
  return rendered === "" ? "" : `?${rendered}`;
}

interface RequestOptions {
  method: "GET" | "POST" | "PATCH" | "DELETE";
  query?: Record<string, QueryValue>;
  body?: unknown;
}

/**
 * Performs one request and maps everything that can go wrong onto `Result`.
 *
 * The single `as T` below is the declared trust boundary: the wire shape is asserted
 * against the generated tree, which `npm run gen:api` regenerates from the same OpenAPI
 * schema the server serializes from — so the assertion can only break when the schema
 * and the server already disagree, which is a backend bug to fix, not a type to weaken.
 *
 * Failure modes, none of which throw: a rejected `fetch` becomes `network`; a non-2xx
 * becomes the envelope's `ApiError`; a 2xx whose body is not JSON becomes `unknown`;
 * an empty body (the 204 of `DELETE /priority-override`) succeeds with `null`.
 */
async function request<T>(
  path: string,
  options: RequestOptions,
): Promise<Result<T>> {
  const url = `${BASE_URL}${path}${options.query ? toQueryString(options.query) : ""}`;
  const headers: Record<string, string> = {
    Accept: "application/json",
    "X-Actor": actor,
  };
  if (options.body !== undefined) headers["Content-Type"] = "application/json";

  let response: Response;
  try {
    response = await fetch(url, {
      method: options.method,
      headers,
      ...(options.body !== undefined
        ? { body: JSON.stringify(options.body) }
        : {}),
    });
  } catch (cause: unknown) {
    return { ok: false, error: errorFromNetwork(cause) };
  }

  const text = await response.text();
  let parsed: unknown = null;
  let isJson = true;
  if (text.length > 0) {
    try {
      parsed = JSON.parse(text);
    } catch {
      isJson = false;
    }
  }

  if (!response.ok) {
    return {
      ok: false,
      error: errorFromResponse(response.status, isJson ? parsed : null),
    };
  }
  if (!isJson) {
    return {
      ok: false,
      error: {
        kind: "unknown",
        code: "unknown_error",
        backendCode: null,
        message: `HTTP ${response.status} returned a non-JSON body.`,
        details: {},
      },
    };
  }
  return { ok: true, data: parsed as T };
}

/** Encodes one path parameter; codes are business identifiers like `PRJ-01-T02`. */
function segment(value: string): string {
  return encodeURIComponent(value);
}

/**
 * Reads one page of the prioritized queue (`GET /api/v1/queue`).
 *
 * Served from the snapshot read model, so this is the command center's single read.
 * `count` is the total across pages; an empty `items` with `count: 0` is a legitimate
 * answer and belongs to the empty view state, not to an error branch.
 */
export async function getQueue(query?: QueueQuery): Promise<Result<QueuePage>> {
  return request<QueuePage>("/api/v1/queue", {
    method: "GET",
    query: { ...query },
  });
}

/**
 * Reads one project in full (`GET /api/v1/projects/{code}`).
 *
 * The response's `transitions` is the only source of transition buttons; an unknown
 * `code` is `not_found`, and `score: null` means "not scored yet", not zero.
 */
export async function getProject(code: string): Promise<Result<ProjectDetail>> {
  return request<ProjectDetail>(`/api/v1/projects/${segment(code)}`, {
    method: "GET",
  });
}

/**
 * Reads one page of a project's tasks (`GET /api/v1/projects/{code}/tasks`).
 *
 * The default order is the server's triage order; `is_overdue` filters on the same
 * server clock that derives the flag on the rows, so the two cannot disagree.
 */
export async function getProjectTasks(
  code: string,
  query?: TaskQuery,
): Promise<Result<TaskPage>> {
  return request<TaskPage>(`/api/v1/projects/${segment(code)}/tasks`, {
    method: "GET",
    query: { ...query },
  });
}

/**
 * Reads one page of a project's timeline (`GET /api/v1/projects/{code}/activity`).
 *
 * Always newest first — there is no `order_by`. A code matching nothing returns an
 * empty page rather than 404: the trail outlives the row it describes.
 */
export async function getProjectActivity(
  code: string,
  query?: TimelineQuery,
): Promise<Result<ActivityPage>> {
  return request<ActivityPage>(`/api/v1/projects/${segment(code)}/activity`, {
    method: "GET",
    query: { ...query },
  });
}

/**
 * Reads the whole roster's load (`GET /api/v1/team/load`).
 *
 * Not paginated: the roster is five people. `is_overloaded` raises a risk flag on the
 * owner's projects; it never lowers a score.
 */
export async function getTeamLoad(
  query?: TeamLoadQuery,
): Promise<Result<TeamLoad>> {
  return request<TeamLoad>("/api/v1/team/load", {
    method: "GET",
    query: { ...query },
  });
}

/**
 * Executes a workflow transition on a project (`POST .../transition`).
 *
 * `reason` is mandatory when the edge sets `requires_reason` — a data decision the
 * server re-checks, so omitting it is a `validation` error naming `reason`, not a
 * silent move. A stale button list is answered `transition_not_allowed` carrying the
 * legal moves in `error.allowed`; resync from that instead of guessing.
 */
export async function postProjectTransition(
  code: string,
  body: ProjectTransitionIn,
): Promise<Result<ProjectDetail>> {
  return request<ProjectDetail>(
    `/api/v1/projects/${segment(code)}/transition`,
    {
      method: "POST",
      body,
    },
  );
}

/**
 * Executes a workflow transition on a task (`POST /api/v1/tasks/{code}/transition`).
 *
 * Same body and error contract as the project transition, against the task workflow.
 * The project's derived health changes are delivered later as stream events, never in
 * this response.
 */
export async function postTaskTransition(
  code: string,
  body: TaskTransitionIn,
): Promise<Result<TaskItem>> {
  return request<TaskItem>(`/api/v1/tasks/${segment(code)}/transition`, {
    method: "POST",
    body,
  });
}

/**
 * Forces a project's queue position on the record (`POST .../priority-override`).
 *
 * Exactly one of `position` / `boost` must be set and `reason` is a constraint, not a
 * convention — violating either is a `validation` error. The returned `score.value` is
 * unchanged by the override; render both so the disagreement stays visible.
 */
export async function postPriorityOverride(
  code: string,
  body: PriorityOverrideIn,
): Promise<Result<OverrideResult>> {
  return request<OverrideResult>(
    `/api/v1/projects/${segment(code)}/priority-override`,
    {
      method: "POST",
      body,
    },
  );
}

/**
 * Lifts a project's manual override (`DELETE .../priority-override`).
 *
 * Idempotent: revoking when nothing is in force is the same 204, so a retry after a
 * dropped response is safe. Success carries no body — `data` is `null`.
 */
export async function deletePriorityOverride(
  code: string,
): Promise<Result<null>> {
  return request<null>(`/api/v1/projects/${segment(code)}/priority-override`, {
    method: "DELETE",
  });
}

/**
 * Resolves one open blocker (`POST /api/v1/blockers/{id}/resolve`).
 *
 * `resolution` is required and non-empty. Resolving an already-resolved blocker is
 * `conflicting_state` with `details.current = "resolved"` — a genuine second attempt,
 * checked under a row lock.
 */
export async function postBlockerResolve(
  blockerId: number,
  body: BlockerResolveIn,
): Promise<Result<Blocker>> {
  return request<Blocker>(`/api/v1/blockers/${blockerId}/resolve`, {
    method: "POST",
    body,
  });
}

/**
 * Appends a chronological note to a project, or to one of its tasks
 * (`POST .../notes`). Nothing parses a note's text; it is not a substitute for a
 * blocker.
 */
export async function postProjectNote(
  code: string,
  body: NoteIn,
): Promise<Result<NoteView>> {
  return request<NoteView>(`/api/v1/projects/${segment(code)}/notes`, {
    method: "POST",
    body,
  });
}

/**
 * Raises a blocker against a project, or against one of its tasks via `task_code`
 * (`POST .../blockers`). Raising a blocker does not change any workflow state.
 */
export async function postProjectBlocker(
  code: string,
  body: BlockerCreateIn,
): Promise<Result<Blocker>> {
  return request<Blocker>(`/api/v1/projects/${segment(code)}/blockers`, {
    method: "POST",
    body,
  });
}

/**
 * Adds a task to a project, in the initial state of the task workflow
 * (`POST .../tasks`). A dependency cycle or a `depends_on` code from another project is
 * a `validation` error naming `depends_on`; an unresolvable code is kept as the
 * dependency's `raw_label`, not rejected.
 */
export async function postProjectTask(
  code: string,
  body: TaskCreateIn,
): Promise<Result<TaskItem>> {
  return request<TaskItem>(`/api/v1/projects/${segment(code)}/tasks`, {
    method: "POST",
    body,
  });
}
