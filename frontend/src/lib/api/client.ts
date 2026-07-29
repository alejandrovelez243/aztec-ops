/**
 * Typed client for the Aztec Ops REST API — the only module outside the stream store
 * that may call `fetch` (`docs/standards/PATTERNS_FRONTEND.md` §3).
 *
 * Every function returns a `Result` and never throws: domain rejections arrive as the
 * typed envelope (`docs/API.md` §1.5) and are mapped onto `ApiError` in `./errors.ts`,
 * network failures become `kind: "network"`, and a body that is not JSON becomes
 * `kind: "unknown"` rather than a `SyntaxError` in a component.
 *
 * Authentication: every request presents `Authorization: Bearer <access>` from the
 * stored session and travels with `credentials: "include"` so the HttpOnly access
 * cookie — the `EventSource`'s only possible credential — stays set. A stale access
 * token is renewed through a single-flight `POST /auth/token/refresh`, plus one
 * retry after an unexpected 401 (a token can expire in flight). When the refresh
 * token itself is rejected, the session is cleared and `aztec:session-expired` is
 * dispatched on `window`; the shell owns the redirect, this module never navigates.
 */
import {
  clearSession,
  isAccessFresh,
  loadSession,
  saveSession,
  type StoredSession,
} from "../auth/tokens";
import { errorFromNetwork, errorFromResponse, type ApiError } from "./errors";
import type { components } from "./types";
import type {
  AccessGrant,
  ActivityPage,
  Blocker,
  BlockerCreateIn,
  BlockerResolveIn,
  CredentialsIn,
  NoteIn,
  RefreshIn,
  NoteView,
  OverrideResult,
  PortfolioActivityQuery,
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
  WorkflowCatalog,
  TimelineQuery,
  TokenPair,
} from "./domain";

/**
 * The outcome of one request: either the typed payload or the typed failure.
 *
 * Callers narrow with `if (!result.ok)`; there is no third "threw" path to handle.
 */
export type Result<T> = { ok: true; data: T } | { ok: false; error: ApiError };

/**
 * Body of `PATCH /api/v1/projects/{code}`, read straight off the generated tree.
 *
 * Absent and `null` are different instructions: an omitted key leaves the column alone, an
 * explicit `null` clears a nullable one. That is why a caller builds this object key by key
 * and never spreads a form over it — spreading turns "no lo toques" into "bórralo".
 */
export type ProjectUpdateIn = components["schemas"]["ProjectUpdateIn"];

/** Query-string values the API accepts; repeatable filters arrive as lists and OR. */
type QueryValue =
  string | number | boolean | readonly string[] | null | undefined;

/**
 * Where the API is, from where this code happens to be running.
 *
 * Two callers, two answers: a server render reaches the API by its compose
 * service name over the internal network, while the browser reaches the same
 * process through its published port on the host. Branching on `import.meta.env.SSR`
 * rather than relying on `INTERNAL_API_URL` being undefined in the browser —
 * which is true, since Astro only exposes `PUBLIC_*` — states the intent instead
 * of depending on an absence.
 */
const BASE_URL: string = import.meta.env.SSR
  ? (import.meta.env.INTERNAL_API_URL ??
    import.meta.env.PUBLIC_API_URL ??
    "http://localhost:8000")
  : (import.meta.env.PUBLIC_API_URL ?? "http://localhost:8000");

/** Event dispatched on `window` when the refresh token itself is rejected. */
export const SESSION_EXPIRED_EVENT = "aztec:session-expired";

/** The one in-flight refresh, so concurrent stale requests share it. */
let refreshInFlight: Promise<string | null> | null = null;

/**
 * Returns a presentable access token, refreshing it first when stale.
 *
 * Single-flight: concurrent callers await the same refresh, so a burst of
 * requests at expiry produces one `POST /auth/token/refresh`, not one each.
 * Returns `null` when signed out or when the refresh was refused — in which
 * case the session has been cleared and {@link SESSION_EXPIRED_EVENT} fired.
 *
 * Exported for the stream store: an `EventSource` cannot carry a header, so
 * the store calls this before connecting to arrive with a fresh access cookie
 * (the refresh response re-sets it).
 */
export async function ensureFreshAccess(): Promise<string | null> {
  if (import.meta.env.SSR) {
    // Server render: the credential is whatever the middleware pulled off this
    // request's mirror cookie. Nothing to refresh — the refresh token stays in
    // the browser by design (see src/middleware.ts).
    const { getServerToken } = await import("../auth/server-token");
    return getServerToken();
  }
  const session = loadSession();
  if (session === null) return null;
  if (isAccessFresh(session)) return session.access;
  refreshInFlight ??= refreshAccess(session);
  try {
    return await refreshInFlight;
  } finally {
    refreshInFlight = null;
  }
}

async function refreshAccess(session: StoredSession): Promise<string | null> {
  const result = await postRefreshToken({ refresh: session.refresh });
  if (!result.ok) {
    // Only a rejected credential ends the session; a network blip must not
    // sign the operator out of a tab that will reconnect on its own.
    if (result.error.kind === "auth" || result.error.kind === "validation") {
      clearSession();
      if (typeof window !== "undefined") {
        window.dispatchEvent(new CustomEvent(SESSION_EXPIRED_EVENT));
      }
    }
    return null;
  }
  const grant = result.data;
  saveSession({
    access: grant.access,
    refresh: session.refresh,
    expiresAt: Date.now() + grant.expires_in * 1000,
    actor: {
      alias: grant.actor.alias,
      label: grant.actor.label,
      role: grant.actor.role ?? null,
    },
    isOpsLead: grant.is_ops_lead,
  });
  return grant.access;
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
  /** Skip the bearer token — only the token-obtain and refresh routes themselves. */
  skipAuth?: boolean;
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

  for (let attempt = 0; ; attempt += 1) {
    const headers: Record<string, string> = { Accept: "application/json" };
    if (options.body !== undefined)
      headers["Content-Type"] = "application/json";
    if (options.skipAuth !== true) {
      const access = await ensureFreshAccess();
      if (access !== null) headers["Authorization"] = `Bearer ${access}`;
    }

    let response: Response;
    try {
      response = await fetch(url, {
        method: options.method,
        headers,
        // The token-obtain and refresh responses set the HttpOnly access
        // cookie the EventSource authenticates with; the API is a different
        // origin in development, so the cookie only sticks when every request
        // is credentialed.
        credentials: "include",
        ...(options.body !== undefined
          ? { body: JSON.stringify(options.body) }
          : {}),
      });
    } catch (cause: unknown) {
      return { ok: false, error: errorFromNetwork(cause) };
    }

    // One retry behind a forced refresh: the freshness check and the server's
    // clock can disagree by the width of a request, so a 401 on a token we
    // believed fresh means "refresh and present again", exactly once.
    if (response.status === 401 && options.skipAuth !== true && attempt === 0) {
      const session = loadSession();
      if (session !== null) {
        saveSession({ ...session, expiresAt: 0 });
        continue;
      }
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
}

/** Encodes one path parameter; codes are business identifiers like `PRJ-01-T02`. */
function segment(value: string): string {
  return encodeURIComponent(value);
}

// --- Authentication -----------------------------------------------------------

/**
 * Exchanges credentials for a token pair (`POST /api/v1/auth/token`).
 *
 * `skipAuth` because obtaining a token cannot require one. The response also
 * sets the HttpOnly access cookie the `EventSource` authenticates with; a 401
 * here is `invalid_credentials` — one answer for unknown user, wrong password
 * and inactive account, so the login form cannot enumerate accounts.
 */
export async function postToken(
  body: CredentialsIn,
): Promise<Result<TokenPair>> {
  return request<TokenPair>("/api/v1/auth/token", {
    method: "POST",
    body,
    skipAuth: true,
  });
}

/**
 * Renews an access token (`POST /api/v1/auth/token/refresh`) and re-sets the
 * access cookie. Reachable precisely when the access token has expired; the
 * refresh token in the body is the credential.
 */
export async function postRefreshToken(
  body: RefreshIn,
): Promise<Result<AccessGrant>> {
  return request<AccessGrant>("/api/v1/auth/token/refresh", {
    method: "POST",
    body,
    skipAuth: true,
  });
}

/**
 * Clears the access cookie (`POST /api/v1/auth/logout`). Idempotent — a retry
 * after a dropped response gets the same 204 — and the caller is expected to
 * drop its stored pair regardless of this call's outcome.
 */
export async function postLogout(): Promise<Result<null>> {
  return request<null>("/api/v1/auth/logout", { method: "POST" });
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
 * Reads one page of the whole portfolio's trail (`GET /api/v1/activity`).
 *
 * Same item type as a project timeline and the same order — newest first, with no
 * `order_by` — so one component renders both. `count` is the total matching the
 * facets, which is what the caller derives its page numbers from: the response
 * carries no next/prev links because paging belongs in the URL beside the filters
 * (`backend/apps/shared/pagination.py`).
 *
 * No facet is validated against its vocabulary: a `verb`, `origin`, `entity_type`
 * or `actor` this deployment does not know returns an empty page, never a 422, so
 * a bookmarked filter cannot break a read-only screen.
 */
export async function getPortfolioActivity(
  query?: PortfolioActivityQuery,
): Promise<Result<ActivityPage>> {
  return request<ActivityPage>("/api/v1/activity", {
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
 * Reads the shape of every configured workflow (`GET /api/v1/workflows`).
 *
 * This is what lets a board draw a column for a state nobody currently occupies:
 * without it, columns can only be derived from the states projects happen to sit
 * in, so an empty "Bloqueado" has no column — which means the board cannot say
 * nothing is blocked, and a card has nowhere to be dropped.
 *
 * It carries no transitions, deliberately. Legality still comes from a project's
 * own `transitions`; this answers "which states exist, in which order", never
 * "which move is allowed" (`docs/API.md` §2.18).
 */
export async function getWorkflows(): Promise<Result<WorkflowCatalog>> {
  return request<WorkflowCatalog>("/api/v1/workflows", { method: "GET" });
}

/**
 * Applies a partial edit to a project (`PATCH /api/v1/projects/{code}`).
 *
 * `workflow_state` is not a field of this payload under any name — a state moves through
 * {@link postProjectTransition} so it is validated against `WorkflowTransition` — and an
 * edit that changes nothing is accepted without writing an event.
 *
 * The response is the project **re-read after the write**, so the risk flags computed on
 * read (`docs/adr/0011`) already reflect the edit: filling `next_step` returns the project
 * without its `NO_NEXT_STEP` flag, and no second GET is needed to see that.
 */
export async function patchProject(
  code: string,
  body: ProjectUpdateIn,
): Promise<Result<ProjectDetail>> {
  return request<ProjectDetail>(`/api/v1/projects/${segment(code)}`, {
    method: "PATCH",
    body,
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
