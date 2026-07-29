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
  Catalog,
  ClientDirectory,
  CredentialsIn,
  Member,
  MemberCreateIn,
  MemberPasswordIn,
  MemberUpdateIn,
  NoteIn,
  RefreshIn,
  RoleCreateIn,
  RoleListQuery,
  RoleUpdateIn,
  StateCreateIn,
  StateUpdateIn,
  TransitionCreateIn,
  TransitionUpdateIn,
  WorkflowAssignmentIn,
  WorkflowCreateIn,
  WorkflowShape,
  WorkflowUpdateIn,
  NoteView,
  OverrideResult,
  PortfolioActivityQuery,
  PriorityOverrideIn,
  ProjectCreateIn,
  ProjectDetail,
  ProjectTransitionIn,
  QueuePage,
  QueueQuery,
  TaskCreateIn,
  TaskDetail,
  TaskItem,
  TaskPage,
  TaskQuery,
  TaskTransitionIn,
  TaxonomyRef,
  TaskUpdateIn,
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
  method: "GET" | "POST" | "PATCH" | "PUT" | "DELETE";
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
 * Reads the roster (`GET /api/v1/team/load`).
 *
 * Not paginated. `is_overloaded` raises a risk flag on the owner's projects; it never lowers
 * a score. `order_by` outside the server's allowlist is a `validation` error carrying the
 * allowed names in `error.details.allowed` — resync from that rather than guessing.
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
 * Reads every active taxonomy list (`GET /api/v1/catalog`).
 *
 * One document rather than six requests, because a form needs all of its pickers before it can
 * draw itself. Every list arrives in the order an operator arranged it; render `label`, send
 * `code`, and never compare against the Spanish.
 */
export async function getCatalog(): Promise<Result<Catalog>> {
  return request<Catalog>("/api/v1/catalog", { method: "GET" });
}

/**
 * Reads the counterparties a project can be registered against (`GET /api/v1/clients`).
 *
 * Not part of `getCatalog` because a client is a portfolio aggregate rather than one of the
 * operator-editable taxonomies that document publishes — serving it there would make the catalog
 * context read a model another context owns.
 *
 * Retired counterparties are absent, so an empty `items` is a legitimate answer and means the
 * operation has nobody to register a project against yet. The creating surface owes that its own
 * copy: a project cannot be created without a client, so an empty directory is a dead button with
 * a reason, never a picker with no options.
 */
export async function getClients(): Promise<Result<ClientDirectory>> {
  return request<ClientDirectory>("/api/v1/clients", { method: "GET" });
}

/**
 * Reads every role, retired ones included (`GET /api/v1/catalog/roles`).
 *
 * Ops lead. Separate from `getCatalog` on purpose: that one feeds the **pickers**, where a
 * retired value must never appear, and this one feeds the editor, which cannot offer "restore"
 * for rows it refuses to show. `status` defaults to `all`.
 */
export async function getRoles(
  query?: RoleListQuery,
): Promise<Result<TaxonomyRef[]>> {
  return request<TaxonomyRef[]>("/api/v1/catalog/roles", {
    method: "GET",
    query: { ...query },
  });
}

/**
 * Adds a role to the vocabulary (`POST /api/v1/catalog/roles`).
 *
 * Ops lead only, and the only taxonomy writable from the product at all — the other five are
 * decisions about how the business works and are made in the admin. `code` is permanent and is
 * what people classified under this role carry; `label` is the Spanish that may be fixed later.
 * A taken code is a `conflict`, **including a retired role's**: restore it rather than create a
 * second one.
 */
export async function postRole(
  body: RoleCreateIn,
): Promise<Result<TaxonomyRef>> {
  return request<TaxonomyRef>("/api/v1/catalog/roles", {
    method: "POST",
    body,
  });
}

/**
 * Renames, retires or restores a role (`PATCH /api/v1/catalog/roles/{code}`).
 *
 * Absent means untouched. `is_active: false` takes it out of the pickers and leaves everybody
 * already classified under it exactly as they are — nothing is deleted, so nobody is silently
 * unclassified.
 */
export async function patchRole(
  code: string,
  body: RoleUpdateIn,
): Promise<Result<TaxonomyRef>> {
  return request<TaxonomyRef>(`/api/v1/catalog/roles/${segment(code)}`, {
    method: "PATCH",
    body,
  });
}

/**
 * Registers a person on the roster (`POST /api/v1/team/members`).
 *
 * Ops lead only — a `permission` error otherwise. `code` is permanent: it is what every event
 * and activity record will name this person by, so there is no rename afterwards. A taken code
 * is a `conflict`, and omitting `password` is normal: the person is assignable immediately and
 * cannot sign in until one is set.
 */
export async function postMember(
  body: MemberCreateIn,
): Promise<Result<Member>> {
  return request<Member>("/api/v1/team/members", { method: "POST", body });
}

/**
 * Edits a person (`PATCH /api/v1/team/members/{code}`).
 *
 * Absent means untouched; explicit `null` on `role` unclassifies them. Sending values the
 * person already has is a successful no-op that writes no history. `is_active: true` is how a
 * retired person is restored.
 */
export async function patchMember(
  code: string,
  body: MemberUpdateIn,
): Promise<Result<Member>> {
  return request<Member>(`/api/v1/team/members/${segment(code)}`, {
    method: "PATCH",
    body,
  });
}

/**
 * Retires a person (`DELETE /api/v1/team/members/{code}`), deleting nothing.
 *
 * The effect is `is_active = false`: the tasks and projects that name them are untouched.
 * Idempotent, so a retry after a dropped response is safe. Retiring the signed-in account is
 * refused with a `permission` error — it would end the caller's own session.
 */
export async function deleteMember(code: string): Promise<Result<null>> {
  return request<null>(`/api/v1/team/members/${segment(code)}`, {
    method: "DELETE",
  });
}

/**
 * Replaces a person's password (`POST /api/v1/team/members/{code}/password`).
 *
 * An administrative reset: no current password is asked for. A refused password is a
 * `validation` error whose `details.fields.password` lists **every** rule it broke, which is
 * what the form renders — showing only the first makes people retry one rule at a time.
 */
export async function postMemberPassword(
  code: string,
  body: MemberPasswordIn,
): Promise<Result<Member>> {
  return request<Member>(`/api/v1/team/members/${segment(code)}/password`, {
    method: "POST",
    body,
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
 * It also carries `transitions`: the arrows an operator drew between those
 * states, which is what `/workflows` draws the graph from. They are a
 * description of the **configuration**, never a permission — whether the record
 * on screen may take one is computed per record against the state it is in, the
 * transition's guard and its `requires_fields`, and stays on the project's or
 * task's own `transitions` (`docs/API.md` §2.2). Acting on an edge published
 * here without asking earns a typed 409 carrying the moves the record may
 * actually take, which is the same failure mode as a stale button
 * (`docs/API.md` §2.18).
 */
export async function getWorkflows(): Promise<Result<WorkflowCatalog>> {
  return request<WorkflowCatalog>("/api/v1/workflows", { method: "GET" });
}

/**
 * Reads the guard codes an edge may name (`GET /api/v1/workflows/guards`).
 *
 * Ops lead, like the writes it accompanies. Served from the backend registry rather than
 * listed here, which is what keeps CLAUDE.md rule 8 true across the wire: a guard added as
 * one function plus one decorator appears in the transition editor with no frontend edit.
 */
export async function getWorkflowGuards(): Promise<Result<string[]>> {
  return request<string[]>("/api/v1/workflows/guards", { method: "GET" });
}

/**
 * Creates a lifecycle (`POST /api/v1/workflows`).
 *
 * Ops lead only. The graph arrives **empty** — states and transitions are added afterwards,
 * one request each — so the caller must walk the operator into its first state rather than
 * leaving them at a blank diagram. A taken `code` is a `conflicting_state`, and so is an
 * `engagement_types` entry already bound to another graph for the same kind of record:
 * resolution has to be deterministic, so a type names exactly one lifecycle per kind.
 */
export async function postWorkflow(
  body: WorkflowCreateIn,
): Promise<Result<WorkflowShape>> {
  return request<WorkflowShape>("/api/v1/workflows", { method: "POST", body });
}

/**
 * Renames a lifecycle, retires it or puts it back in service
 * (`PATCH /api/v1/workflows/{code}`). Absent means untouched.
 *
 * `code` and `applies_to` are not writable: the first is the address every binding names it
 * by, the second would leave the records already inside governed by a lifecycle claiming to
 * govern something else. `is_active: false` is a retirement, never a delete — the graph stops
 * being offered to new work and keeps resolving for everything already in it.
 */
export async function patchWorkflow(
  code: string,
  body: WorkflowUpdateIn,
): Promise<Result<WorkflowShape>> {
  return request<WorkflowShape>(`/api/v1/workflows/${segment(code)}`, {
    method: "PATCH",
    body,
  });
}

/**
 * Adds a state to a lifecycle (`POST /api/v1/workflows/{code}/states`).
 *
 * `order` omitted **appends**, because a new column belongs at the end of an arrangement
 * somebody already made. The first state of an empty graph becomes its entry node. A code the
 * graph already carries is a `conflicting_state` **including when that state is retired** —
 * restore it instead of creating a second one.
 *
 * Every write on this router answers with the whole graph re-read, so the caller repaints the
 * diagram and the tables from one authoritative shape instead of guessing at the edit.
 */
export async function postWorkflowState(
  code: string,
  body: StateCreateIn,
): Promise<Result<WorkflowShape>> {
  return request<WorkflowShape>(`/api/v1/workflows/${segment(code)}/states`, {
    method: "POST",
    body,
  });
}

/**
 * Edits a state (`PATCH /api/v1/workflows/{code}/states/{state}`). Absent means untouched.
 *
 * The state's own `code` is not writable — every project and task standing on it holds that
 * value, and renaming is what `label` is for. `is_active` accepts only `true`, which restores
 * a retired state; retiring is {@link deleteWorkflowState}, because it can be refused by
 * records this request knows nothing about.
 */
export async function patchWorkflowState(
  code: string,
  stateCode: string,
  body: StateUpdateIn,
): Promise<Result<WorkflowShape>> {
  return request<WorkflowShape>(
    `/api/v1/workflows/${segment(code)}/states/${segment(stateCode)}`,
    { method: "PATCH", body },
  );
}

/**
 * Retires a state (`DELETE /api/v1/workflows/{code}/states/{state}`), deleting nothing.
 *
 * The row survives with `is_active: false` and every arrow touching it is withdrawn in the
 * same transaction. **Refused while records occupy it**, with `conflicting_state` carrying
 * `details.projects` and `details.tasks`: an operator told how many rows to move can act, one
 * told "conflicting state" cannot — so the refusal must be rendered with those numbers.
 */
export async function deleteWorkflowState(
  code: string,
  stateCode: string,
): Promise<Result<WorkflowShape>> {
  return request<WorkflowShape>(
    `/api/v1/workflows/${segment(code)}/states/${segment(stateCode)}`,
    { method: "DELETE" },
  );
}

/**
 * Declares a move between two states of one graph
 * (`POST /api/v1/workflows/{code}/transitions`).
 *
 * Both endpoints are resolved inside the workflow in the path, so an edge across two
 * lifecycles is not expressible. `guard` must be one of {@link getWorkflowGuards}'s codes.
 * Declaring an edge changes no record: whether a given project or task may take it is still
 * decided per record. An existing pair is a `conflicting_state` — restore it with
 * {@link patchWorkflowTransition} rather than creating a second row.
 */
export async function postWorkflowTransition(
  code: string,
  body: TransitionCreateIn,
): Promise<Result<WorkflowShape>> {
  return request<WorkflowShape>(
    `/api/v1/workflows/${segment(code)}/transitions`,
    { method: "POST", body },
  );
}

/**
 * Edits a move, or restores a withdrawn one
 * (`PATCH .../transitions/{from}/{to}`). Absent means untouched.
 *
 * The ordered pair is the address and is not editable: an edge *is* its endpoints, so
 * repointing an arrow is withdrawing one move and declaring another. `requires_fields` is sent
 * whole, and `is_active: true` is the only way a withdrawn move comes back.
 */
export async function patchWorkflowTransition(
  code: string,
  fromState: string,
  toState: string,
  body: TransitionUpdateIn,
): Promise<Result<WorkflowShape>> {
  return request<WorkflowShape>(
    `/api/v1/workflows/${segment(code)}/transitions/${segment(fromState)}/${segment(toState)}`,
    { method: "PATCH", body },
  );
}

/**
 * Withdraws a move (`DELETE .../transitions/{from}/{to}`), deleting nothing.
 *
 * The row survives with `is_active: false` and disappears from `transitions` on the next read,
 * because a withdrawn move is not a drawable arrow. Withdrawing the last move **out of** a
 * state is allowed and is how a terminal state is declared.
 */
export async function deleteWorkflowTransition(
  code: string,
  fromState: string,
  toState: string,
): Promise<Result<WorkflowShape>> {
  return request<WorkflowShape>(
    `/api/v1/workflows/${segment(code)}/transitions/${segment(fromState)}/${segment(toState)}`,
    { method: "DELETE" },
  );
}

/**
 * Registers a project in the portfolio (`POST /api/v1/projects`, 201).
 *
 * Neither `code` nor `workflow_state` is a body field. The service allocates the next `PRJ-NN`
 * inside the creating transaction — a code chosen here could collide with one an already-published
 * event names, and an event cannot be un-published — and places the project on the initial state of
 * the workflow its **engagement type** binds to, which is why picking that type is a lifecycle
 * decision and not a label.
 *
 * Failure modes worth branching on: `conflicting_state` is the allocation losing a race with a
 * simultaneous creation and is safe to retry once, exactly because nothing was written;
 * `validation` names the offending fields, `business_value` below zero and a `start_date` after
 * `target_date` among them. The response is the project as its detail screen reads it, so the
 * caller has its `transitions` and its freshly computed risk flags without a second GET — a
 * project created with neither `next_step` nor `target_date` comes back already flagged.
 */
export async function postProject(
  body: ProjectCreateIn,
): Promise<Result<ProjectDetail>> {
  return request<ProjectDetail>("/api/v1/projects", { method: "POST", body });
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
 * Puts a project on a named lifecycle (`PUT /api/v1/projects/{code}/workflow`).
 *
 * Ops lead only, like the priority override. It changes the graph and never the state: the
 * project keeps the state `code` it was standing on and is repointed at that state inside the
 * target, so this cannot reach a state no edge leads to — moving is still
 * {@link postProjectTransition}.
 *
 * `conflicting_state` when the target has no active state carrying the project's current state
 * code, with `details.available` listing the ones it does offer, and when the target is retired
 * (`details.current = "retired"`); `validation_error` on `fields.workflow` when the named graph
 * governs tasks. The response is the project **re-read after the write**, so its `transitions`
 * already come from the new graph.
 */
export async function putProjectWorkflow(
  code: string,
  body: WorkflowAssignmentIn,
): Promise<Result<ProjectDetail>> {
  return request<ProjectDetail>(`/api/v1/projects/${segment(code)}/workflow`, {
    method: "PUT",
    body,
  });
}

/**
 * Stops a project following its own lifecycle (`DELETE /api/v1/projects/{code}/workflow`).
 *
 * Removes the *assignment*, never a workflow and never the project: what comes back is the
 * project following whatever the binding ladder hands it, with `workflow.source` flipped to
 * `INHERITED`. The inherited graph is compatibility-checked exactly like a named one, so this
 * carries the same `conflicting_state` — "heredado" is not a synonym for "safe".
 */
export async function deleteProjectWorkflow(
  code: string,
): Promise<Result<ProjectDetail>> {
  return request<ProjectDetail>(`/api/v1/projects/${segment(code)}/workflow`, {
    method: "DELETE",
  });
}

/**
 * Reads one task in full (`GET /api/v1/tasks/{code}`).
 *
 * The screen-shaped read: the project it belongs to, its legal `transitions` and its
 * comments arrive together, so the detail never renders half of itself while a second
 * request is in flight. `transitions` is the only source of the state buttons and an
 * empty list is a legitimate answer — the task sits in a terminal state.
 *
 * An unknown `code` is `not_found`; the task list's `TaskItem` is a narrower shape and
 * cannot stand in for this one.
 */
export async function getTask(code: string): Promise<Result<TaskDetail>> {
  return request<TaskDetail>(`/api/v1/tasks/${segment(code)}`, {
    method: "GET",
  });
}

/**
 * Body of `PATCH /api/v1/tasks/{code}`, as the wire actually accepts it.
 *
 * `Partial<>` rather than the generated alias, and the difference is not cosmetic:
 * `TaskUpdateIn`'s string fields carry server-side defaults, which openapi-typescript
 * emits as **required** — unlike `ProjectUpdateIn`, whose `str | None = None` fields
 * come out optional. Taken literally, reassigning a task would mean resending its
 * title, detail, last progress and priority, and any of those going stale between the
 * read and the write would silently overwrite a colleague's edit.
 *
 * `Partial` is homomorphic, so under `exactOptionalPropertyTypes` this yields
 * `title?: string` *without* `| undefined`: omission stays the only way to say
 * "untouched", and `{ assignee: null }` still typechecks and still serializes as JSON
 * `null`. A caller therefore cannot write `{ title: undefined }` and mean "leave
 * alone" — it omits the key. Aligning the backend to `str | None = None` retires this
 * alias.
 */
export type TaskPatch = Partial<TaskUpdateIn>;

/**
 * Applies a partial edit to a task (`PATCH /api/v1/tasks/{code}`).
 *
 * Absent means untouched and an explicit `null` clears — which is how a task is
 * unassigned — so a caller builds this body key by key and never spreads a form over
 * it. `workflow_state` is not a field of the payload: a state moves through
 * {@link postTaskTransition}.
 *
 * The response is the task **re-read after the write**, `transitions` included, so the
 * screen that issued the edit has everything it needs to redraw without a second GET.
 */
export async function patchTask(
  code: string,
  body: TaskPatch,
): Promise<Result<TaskDetail>> {
  return request<TaskDetail>(`/api/v1/tasks/${segment(code)}`, {
    method: "PATCH",
    body,
  });
}

/**
 * Removes one task from the operation (`DELETE /api/v1/tasks/{code}`).
 *
 * A soft delete: the row's `is_archived` flips to true, so the task leaves every list
 * and its own `GET` answers 404, while its blockers, its comments and every dependency
 * edge naming it survive. The undo is therefore an ordinary edit —
 * `patchTask(code, { is_archived: false })` — not a re-creation.
 *
 * Success carries no body, so `data` is `null`. An unknown code is `not_found`; a code
 * already removed is `not_found` too, because the read it goes through no longer sees
 * it, which makes a retry after a dropped response report "ya no existe" rather than
 * failing silently.
 */
export async function deleteTask(code: string): Promise<Result<null>> {
  return request<null>(`/api/v1/tasks/${segment(code)}`, {
    method: "DELETE",
  });
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
 * Puts a task on a named lifecycle (`PUT /api/v1/tasks/{code}/workflow`).
 *
 * The task counterpart of {@link putProjectWorkflow}, with the same contract: ops lead only, the
 * graph changes and the state code does not, and the same `conflicting_state` when the target has
 * no active state carrying the task's current one. The response is the task re-read after the
 * write, `transitions` included.
 */
export async function putTaskWorkflow(
  code: string,
  body: WorkflowAssignmentIn,
): Promise<Result<TaskDetail>> {
  return request<TaskDetail>(`/api/v1/tasks/${segment(code)}/workflow`, {
    method: "PUT",
    body,
  });
}

/**
 * Stops a task following its own lifecycle (`DELETE /api/v1/tasks/{code}/workflow`).
 *
 * The task counterpart of {@link deleteProjectWorkflow}: the assignment is removed and the task
 * goes back to the graph its project's engagement type binds, checked for compatibility exactly
 * like a named one.
 */
export async function deleteTaskWorkflow(
  code: string,
): Promise<Result<TaskDetail>> {
  return request<TaskDetail>(`/api/v1/tasks/${segment(code)}/workflow`, {
    method: "DELETE",
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
