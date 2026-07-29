/**
 * The Equipo island: the roster stays honest, and an ops lead can change it.
 *
 * **It never patches numbers out of an envelope**, because no envelope carries them — load is
 * derived from task rows when the endpoint is called. A watched topic therefore means "the
 * answer may have changed" and the island refetches, debounced so a burst of task events is one
 * request. Out-of-order answers are dropped by sequence, so a slow early response can never
 * overwrite a fast later one.
 *
 * **Filtering and sorting are the server's**, not this module's. Every control writes into the
 * URL and refetches; nothing here reorders rows it was handed. That is what makes the address
 * bar a shareable statement of what is on screen instead of a decoration beside it.
 *
 * **Every write goes through the API and is followed by a refetch**, never by patching the row
 * optimistically: the load figures depend on rows this surface does not own, and a capacity
 * edit changes who reads as overloaded. Guessing would put a number on screen that no read
 * would reproduce.
 */
import {
  deleteMember,
  getCatalog,
  getRoles,
  getTeamLoad,
  patchMember,
  patchRole,
  postMember,
  postMemberPassword,
  postRole,
} from "../../lib/api/client";
import type { Result } from "../../lib/api/client";
import type {
  Member,
  MemberCreateIn,
  MemberUpdateIn,
  TaxonomyRef,
  TeamLoad,
  TeamLoadEntry,
} from "../../lib/api/domain";
import type { ApiError } from "../../lib/api/errors";
import { avatarHue, getOperator, initials } from "../../lib/auth/session";
import {
  onReset,
  onStatus,
  retry,
  subscribe,
  type Envelope,
  type Status,
} from "../../lib/stream/store";
import type { Topic } from "../../lib/stream/topics";
import { toast } from "../../lib/toast";
import { mountMenus } from "../../lib/ui/menu";
import {
  applyMenuChoice,
  findMenuSelect,
  menuOptions,
  menuSelectValue,
  setMenuOptions,
  setMenuValue,
} from "../../lib/ui/menu-select";
import { assertNever, toViewState, type ViewState } from "../../lib/view-state";
import {
  alertTone,
  barPercent,
  DEFAULT_QUERY,
  EMPTY_MESSAGE,
  EMPTY_ROSTER_MESSAGE,
  errorCopy,
  formatClock,
  formatCount,
  formatPoints,
  formatRole,
  formatStamp,
  formatStatus,
  formatSummary,
  formatUtilization,
  isFiltered,
  loadTone,
  orderByOf,
  paramsFromQuery,
  queryFromParams,
  statusTone,
  toggledSort,
  TONE_CLASSES,
  urgentTone,
  type RosterQuery,
  type SortField,
  type ToneClass,
} from "./table";

/**
 * Topics that can change something this surface renders.
 *
 * The task topics move the load; the project topics move `projects_owned`; the member topics
 * move the roster itself, including edits another operator made in another tab. A rendered
 * figure whose event nobody listens for goes stale in silence, which is the one failure an
 * operations board must not have.
 */
const WATCHED_TOPICS: readonly Topic[] = [
  "task.created",
  "task.updated",
  "task.state_changed",
  "project.created",
  "project.updated",
  "member.created",
  "member.updated",
  "member.activation_changed",
];

/** A burst of events during one drain must cost one request, not one each. */
const REFRESH_DEBOUNCE_MS = 600;

/** Typing is not a request per keystroke; this is the pause that means "I have finished". */
const SEARCH_DEBOUNCE_MS = 300;

/** Which arm of the view-state union the region is currently rendering. */
type Arm = ViewState<TeamLoad>["kind"];

/** What the member dialog is currently for. The two modes differ, so they are not one flag. */
type DialogMode =
  | { readonly kind: "create" }
  | { readonly kind: "edit"; readonly alias: string };

/**
 * Wires the surface and returns its teardown.
 *
 * @param root - The `[data-team-region]` element the page rendered.
 */
export function mountRoster(root: HTMLElement): () => void {
  const cleanups: Array<() => void> = [];
  let refreshTimer: number | null = null;
  let searchTimer: number | null = null;
  /** Monotonic request id; a response whose id is stale is discarded, never rendered. */
  let requestSeq = 0;
  let status: Status = "connecting";
  let lastEventAt: string | null = null;
  /** True while the last refresh failed, so a broken API toasts once and not per event. */
  let refreshFailed = false;
  /** True once the connection dropped: what the stream missed can only come from a refetch. */
  let missedEvents = false;
  let query: RosterQuery = queryFromParams(
    new URLSearchParams(window.location.search),
  );
  let dialogMode: DialogMode = { kind: "create" };
  let passwordTarget = "";
  /**
   * The vocabulary every picker on this surface draws from, seeded from the server's render and
   * replaced wholesale after each role write. One list, so the toolbar facet, the member form
   * and the roles dialog cannot show three different answers.
   */
  let roles: readonly TaxonomyRef[] = readRenderedRoles(root);
  /**
   * The editor's list: everything, retired included. Read only when the roles dialog is opened,
   * because it is the only surface with any use for a row nobody may pick.
   */
  let editable: readonly TaxonomyRef[] = [];

  const isOpsLead = getOperator()?.isOpsLead === true;

  // --- Element access --------------------------------------------------------

  const find = <T extends HTMLElement>(selector: string): T | null =>
    root.querySelector<T>(selector);

  const rowsBody = (): HTMLElement | null => find("[data-rows]");
  const template = (): HTMLTemplateElement | null =>
    find<HTMLTemplateElement>("[data-row-template]");
  const memberDialog = (): HTMLDialogElement | null =>
    find<HTMLDialogElement>("[data-member-dialog]");
  const passwordDialog = (): HTMLDialogElement | null =>
    find<HTMLDialogElement>("[data-password-dialog]");

  function setArm(arm: Arm): void {
    root.dataset.view = arm;
  }

  function hasRows(): boolean {
    return root.querySelector("[data-rows] [data-alias]") !== null;
  }

  function setTone(el: Element, tone: ToneClass): void {
    el.classList.remove(...TONE_CLASSES);
    el.classList.add(tone);
  }

  // --- Rendering -------------------------------------------------------------

  /**
   * Writes one roster row into an existing `<tr>`.
   *
   * Every value goes through the same functions the server rendered with, so a patched row and
   * a freshly rendered one are indistinguishable.
   */
  function applyEntry(row: HTMLElement, entry: TeamLoadEntry): void {
    row.dataset.alias = entry.alias;
    row.dataset.roleCode = entry.role_code ?? "";
    row.dataset.label = entry.label;
    row.dataset.capacity = String(entry.weekly_capacity_points);
    row.dataset.active = String(entry.is_active);
    row.dataset.utilization = String(entry.utilization);
    row.classList.toggle("is-retired", !entry.is_active);

    const avatar = row.querySelector<HTMLElement>('[data-field="avatar"]');
    if (avatar !== null) {
      avatar.textContent = initials(entry.label);
      avatar.style.setProperty("--avatar-h", String(avatarHue(entry.alias)));
    }

    setText(row, "label", entry.label);
    setText(row, "alias", entry.alias);
    setText(row, "role", formatRole(entry.role));
    setText(row, "utilization", formatUtilization(entry.utilization));
    setText(
      row,
      "points",
      formatPoints(entry.load_points, entry.weekly_capacity_points),
    );
    setText(row, "open_tasks", formatCount(entry.open_tasks));
    setText(row, "overdue_tasks", formatCount(entry.overdue_tasks));
    setText(row, "blocked_tasks", formatCount(entry.blocked_tasks));
    setText(row, "urgent_tasks", formatCount(entry.high_or_critical_open));
    setText(row, "projects_owned", formatCount(entry.projects_owned));
    setText(row, "status", formatStatus(entry));

    const bar = row.querySelector<HTMLElement>('[data-field="bar"]');
    if (bar !== null)
      bar.style.setProperty(
        "--bar",
        String(barPercent(entry.utilization) / 100),
      );

    const load = row.querySelector<HTMLElement>(".load");
    if (load !== null) setTone(load, loadTone(entry.is_overloaded));

    toneOf(row, "overdue_tasks", alertTone(entry.overdue_tasks));
    toneOf(row, "blocked_tasks", alertTone(entry.blocked_tasks));
    toneOf(row, "urgent_tasks", urgentTone(entry.high_or_critical_open));
    toneOf(row, "status", statusTone(entry));

    // Both directions exist in the markup; only their visibility and their accessible names
    // move. Nothing here draws an icon.
    showAction(
      row,
      "retire",
      entry.is_active,
      `Retirar a ${entry.label} del equipo`,
    );
    showAction(row, "restore", !entry.is_active, `Restaurar a ${entry.label}`);
    setActionName(row, "edit", `Editar a ${entry.label}`);
    setActionName(row, "password", `Cambiar la contraseña de ${entry.label}`);

    for (const actions of row.querySelectorAll<HTMLElement>(
      "[data-roster-actions]",
    )) {
      actions.hidden = !isOpsLead;
    }
  }

  /** Reveals one direction of the retire/restore pair and names it after the person. */
  function showAction(
    row: HTMLElement,
    field: string,
    visible: boolean,
    name: string,
  ): void {
    const button = row.querySelector<HTMLElement>(`[data-field="${field}"]`);
    if (button === null) return;
    button.hidden = !visible;
    button.setAttribute("aria-label", name);
  }

  /** The tooltip stays generic; the accessible name says who, since a reader has no row. */
  function setActionName(row: HTMLElement, action: string, name: string): void {
    const button = row.querySelector<HTMLElement>(
      `[data-member-action="${action}"]`,
    );
    if (button === null) return;
    button.setAttribute("aria-label", name);
  }

  function setText(row: HTMLElement, field: string, text: string): void {
    const el = row.querySelector<HTMLElement>(`[data-field="${field}"]`);
    if (el !== null) el.textContent = text;
  }

  function toneOf(row: HTMLElement, field: string, tone: ToneClass): void {
    const el = row.querySelector(`[data-field="${field}"]`);
    if (el !== null) setTone(el, tone);
  }

  /**
   * Rebuilds the table body from a fresh answer.
   *
   * Rows are cloned from the template rather than reordered in place, because the server
   * decides both membership and order: a row that survives a sort is not the same row in the
   * same position, and matching them up would be this module re-deriving an order it was
   * handed.
   */
  function renderRows(entries: readonly TeamLoadEntry[]): void {
    const body = rowsBody();
    const source = template();
    if (body === null || source === null) return;

    const fragment = document.createDocumentFragment();
    for (const entry of entries) {
      const clone = source.content.firstElementChild?.cloneNode(true) ?? null;
      if (!(clone instanceof HTMLElement)) continue;
      applyEntry(clone, entry);
      fragment.append(clone);
    }
    body.replaceChildren(fragment);

    const summary = find("[data-summary]");
    if (summary !== null) summary.textContent = formatSummary(entries);
  }

  /** Marks the header that is in force, which is what `aria-sort` announces. */
  function renderSortHeaders(): void {
    for (const header of root.querySelectorAll<HTMLElement>(
      "[data-sort-header]",
    )) {
      const field = header.dataset.sortHeader;
      if (field !== query.sort) {
        header.setAttribute("aria-sort", "none");
        continue;
      }
      header.setAttribute(
        "aria-sort",
        query.descending ? "descending" : "ascending",
      );
    }
  }

  function renderStale(at: string | null): void {
    const clock = find("[data-stale-time]");
    if (clock === null) return;
    const stamp = at ?? root.dataset.fetchedAt ?? null;
    if (stamp === null) {
      clock.textContent = "hora desconocida";
      return;
    }
    clock.textContent = formatClock(stamp);
    clock.title = formatStamp(stamp);
    if (clock instanceof HTMLTimeElement) clock.dateTime = stamp;
  }

  function render(view: ViewState<TeamLoad>): void {
    renderSortHeaders();
    switch (view.kind) {
      case "loading":
        setArm("loading");
        return;
      case "ready":
        renderRows(view.data.items);
        setArm("ready");
        return;
      case "disconnected":
        renderRows(view.data.items);
        // Arm first, then the timestamp: a `role="status"` announces a change it was visible
        // for, so writing the clock into a hidden banner would announce nothing.
        setArm("disconnected");
        renderStale(view.lastEventAt);
        return;
      case "empty": {
        const copy = find("[data-empty-copy]");
        if (copy !== null) copy.textContent = view.message;
        const clear = find("[data-empty-clear]");
        if (clear !== null) clear.hidden = !isFiltered(query);
        setArm("empty");
        return;
      }
      case "error": {
        const copy = find("[data-error-copy]");
        if (copy !== null) copy.textContent = errorCopy(view.code);
        setArm("error");
        return;
      }
      default:
        assertNever(view);
    }
  }

  // --- Reading ---------------------------------------------------------------

  async function fetchAndRender(): Promise<void> {
    const seq = (requestSeq += 1);
    const result = await getTeamLoad({
      q: query.search,
      role: query.role === null ? [] : [query.role],
      status: query.status,
      ...(query.overloaded === null ? {} : { overloaded: query.overloaded }),
      order_by: orderByOf(query),
    });
    if (seq !== requestSeq) return;

    const view = toViewState(result, status, {
      isEmpty: (data) => data.items.length === 0,
      emptyMessage: isFiltered(query) ? EMPTY_MESSAGE : EMPTY_ROSTER_MESSAGE,
      lastEventAt,
    });

    if (view.kind === "error" && hasRows()) {
      if (!refreshFailed) {
        toast({
          kind: "error",
          title: "No pudimos actualizar el equipo",
          detail: errorCopy(view.code),
        });
      }
      refreshFailed = true;
      return;
    }
    refreshFailed = false;
    render(view);
  }

  /** The stream-driven refresh: the rows stay up while it runs, so nothing flashes. */
  function scheduleRefresh(): void {
    if (refreshTimer !== null) window.clearTimeout(refreshTimer);
    refreshTimer = window.setTimeout(() => {
      refreshTimer = null;
      void fetchAndRender();
    }, REFRESH_DEBOUNCE_MS);
  }

  /**
   * Adopts a new question: URL first, then the read.
   *
   * `replaceState` and not `pushState`: narrowing a filter is not a navigation, and a back
   * button that walks through every keystroke is a back button nobody can use to leave.
   */
  function applyQuery(next: RosterQuery): void {
    query = next;
    const params = paramsFromQuery(query);
    const search = params.toString();
    window.history.replaceState(
      null,
      "",
      `${window.location.pathname}${search === "" ? "" : `?${search}`}`,
    );
    void fetchAndRender();
  }

  // --- Writing ---------------------------------------------------------------

  /**
   * Reports a failed write where the operator asked for it.
   *
   * Field errors go to the field the server named — that is the difference between an operator
   * fixing the request and giving up on it — and anything else becomes one line at the foot of
   * the form.
   */
  function showFormErrors(form: HTMLElement, error: ApiError): void {
    let named = false;
    // `fields` exists only on the validation arm, already narrowed to `string[]` by the client:
    // reading it off the union would also read it off a network failure, which has no envelope
    // to have carried one.
    if (error.kind === "validation") {
      for (const [name, messages] of Object.entries(error.fields)) {
        const slot = form.querySelector<HTMLElement>(`[data-error="${name}"]`);
        if (slot === null) continue;
        slot.textContent = messages.join(" ");
        slot.hidden = false;
        named = true;
      }
    }
    if (named) return;
    const summary = form.querySelector<HTMLElement>(
      '[data-field="form-error"]',
    );
    if (summary === null) return;
    summary.textContent = errorCopy(error.code);
    summary.hidden = false;
  }

  function clearFormErrors(form: HTMLElement): void {
    for (const slot of form.querySelectorAll<HTMLElement>("[data-error]")) {
      slot.textContent = "";
      slot.hidden = true;
    }
    const summary = form.querySelector<HTMLElement>(
      '[data-field="form-error"]',
    );
    if (summary !== null) {
      summary.textContent = "";
      summary.hidden = true;
    }
  }

  function openMemberDialog(mode: DialogMode): void {
    const dialog = memberDialog();
    if (dialog === null) return;
    const form = dialog.querySelector<HTMLFormElement>("[data-member-form]");
    if (form === null) return;

    dialogMode = mode;
    form.reset();
    // `form.reset()` cannot reach the role picker — it is a button and a panel, not a form
    // control — so a second open would otherwise start on the previous person's role.
    setFacet(form, "member-role", "");
    clearFormErrors(form);

    const title = form.querySelector<HTMLElement>(
      '[data-field="dialog-title"]',
    );
    const lead = form.querySelector<HTMLElement>('[data-field="dialog-lead"]');
    const codeLine = form.querySelector<HTMLElement>(
      '[data-field="code-line"]',
    );
    const codeValue = form.querySelector<HTMLElement>(
      '[data-field="code-value"]',
    );
    const passwordField = form.querySelector<HTMLElement>(
      '[data-field="password-field"]',
    );

    if (mode.kind === "create") {
      if (title !== null) title.textContent = "Añadir persona";
      if (lead !== null) {
        lead.textContent =
          "El código con el que entra al sistema se genera solo, a partir del nombre.";
      }
      // Nothing to show yet: the server mints the code from the name when it saves.
      if (codeLine !== null) codeLine.hidden = true;
      // A password is offered on registration and never on an edit: replacing a credential is
      // its own action, with its own record and its own dialog.
      if (passwordField !== null) passwordField.hidden = false;
      dialog.showModal();
      return;
    }

    const row = rowFor(mode.alias);
    if (title !== null) {
      title.textContent = `Editar a ${row?.dataset.label ?? mode.alias}`;
    }
    if (lead !== null) {
      lead.textContent =
        "El código no cambia: es lo que nombra a esta persona en cada evento y en la bitácora.";
    }
    if (codeValue !== null) codeValue.textContent = mode.alias;
    if (codeLine !== null) codeLine.hidden = false;
    const label = form.elements.namedItem("label");
    if (label instanceof HTMLInputElement) {
      label.value = row?.dataset.label ?? "";
    }
    // A role retired since this page was rendered is not in the picker; `setFacet` leaves it on
    // "Sin rol" rather than showing a value no option ticks and the server would now reject.
    setFacet(form, "member-role", row?.dataset.roleCode ?? "");
    const capacity = form.elements.namedItem("weekly_capacity_points");
    if (capacity instanceof HTMLInputElement) {
      capacity.value = row?.dataset.capacity ?? "";
    }
    if (passwordField !== null) passwordField.hidden = true;
    dialog.showModal();
  }

  function rowFor(alias: string): HTMLElement | null {
    return root.querySelector<HTMLElement>(
      `[data-alias="${CSS.escape(alias)}"]`,
    );
  }

  async function submitMember(form: HTMLFormElement): Promise<void> {
    clearFormErrors(form);
    const data = new FormData(form);
    const label = String(data.get("label") ?? "").trim();
    // Not in the `FormData`: the role picker is a `MenuSelect`, whose `data-value` is the field.
    const role = menuSelectValue(form, "member-role");
    const capacity = Number(data.get("weekly_capacity_points"));

    const submit = form.querySelector<HTMLButtonElement>(
      '[data-action="submit-member"]',
    );
    markBusy(submit);

    const result: Result<Member> =
      dialogMode.kind === "create"
        ? await postMember({
            label,
            role: role === "" ? null : role,
            weekly_capacity_points: capacity,
            password: passwordOrNull(data.get("password")),
          } satisfies MemberCreateIn)
        : await patchMember(dialogMode.alias, {
            label,
            role: role === "" ? null : role,
            weekly_capacity_points: capacity,
          } satisfies MemberUpdateIn);

    clearBusy(submit);
    if (!result.ok) {
      showFormErrors(form, result.error);
      return;
    }

    memberDialog()?.close();
    toast({
      kind: "success",
      title:
        dialogMode.kind === "create"
          ? // The assigned code is announced here: it is the one thing the operator did not
            // choose and will need, because it is how they find this person in a payload.
            `${result.data.label} ya está en el equipo como ${result.data.alias}`
          : `${result.data.label} actualizada`,
    });
    void fetchAndRender();
  }

  async function submitPassword(form: HTMLFormElement): Promise<void> {
    const problems = form.querySelector<HTMLElement>(
      '[data-field="password-problems"]',
    );
    if (problems !== null) {
      problems.replaceChildren();
      problems.hidden = true;
    }

    const data = new FormData(form);
    const submit = form.querySelector<HTMLButtonElement>(
      '[data-action="submit-password"]',
    );
    markBusy(submit);
    const result = await postMemberPassword(passwordTarget, {
      password: String(data.get("password") ?? ""),
    });
    clearBusy(submit);

    if (!result.ok) {
      renderPasswordProblems(problems, result.error);
      return;
    }

    passwordDialog()?.close();
    form.reset();
    toast({
      kind: "success",
      title: `Contraseña cambiada para ${result.data.label}`,
    });
    void fetchAndRender();
  }

  /** Every rule the server rejected, as its own line — it answers with all of them at once. */
  function renderPasswordProblems(
    list: HTMLElement | null,
    error: ApiError,
  ): void {
    if (list === null) return;
    const named =
      error.kind === "validation" ? (error.fields["password"] ?? []) : [];
    // The surface's own vocabulary when the failure was not about the password itself — a
    // dropped connection has no rule to state.
    const messages = named.length > 0 ? named : [errorCopy(error.code)];
    list.replaceChildren(
      ...messages.map((message) => {
        const item = document.createElement("li");
        item.textContent = message;
        return item;
      }),
    );
    list.hidden = false;
  }

  /**
   * Retires or restores somebody, asking first in the direction that is hard to undo.
   *
   * Only the retirement is confirmed. Restoring somebody is reversible by the same button and
   * costs nothing if it was a misclick, and a confirmation on every action is a confirmation
   * nobody reads by the third one.
   */
  async function toggleActive(alias: string): Promise<void> {
    const row = rowFor(alias);
    const wasActive = row?.dataset.active === "true";
    const label = row?.dataset.label ?? alias;

    if (wasActive) {
      const confirmed = await confirmAction(
        `¿Retirar a ${label}?`,
        "Deja de recibir trabajo nuevo. Sus tareas y sus proyectos siguen exactamente igual, y puedes restaurarla cuando quieras.",
        "Retirar del equipo",
      );
      if (!confirmed) return;
    }

    const result = wasActive
      ? await deleteMember(alias)
      : await patchMember(alias, { is_active: true });

    if (!result.ok) {
      toast({
        kind: "error",
        title: wasActive
          ? `No pudimos retirar a ${label}`
          : `No pudimos restaurar a ${label}`,
        detail: errorCopy(result.error.code),
      });
      return;
    }

    toast({
      kind: "success",
      title: wasActive
        ? `${label} quedó fuera del equipo`
        : `${label} está de vuelta`,
    });
    void fetchAndRender();
  }

  // --- Roles -----------------------------------------------------------------

  /**
   * Redraws the role list and every role picker on the surface from one answer.
   *
   * The catalog is re-read after each write rather than patched, for the same reason the roster
   * is: a role's `order` is the server's, and a list this module reordered would disagree with
   * the next page load. It also keeps the toolbar facet, the member dialog's select and the
   * dialog's own list from ever showing three different vocabularies.
   */
  /**
   * Which arm the roles list is showing.
   *
   * A list that is merely slow and a list that is genuinely empty render the
   * same empty `<ul>`, and an operator reads that as "my roles are gone" — which
   * is exactly what happened. The flag is what separates the two.
   */
  function setRolesState(state: "loading" | "ready" | "empty" | "error"): void {
    const region = find("[data-roles-region]");
    if (region !== null) region.dataset.rolesState = state;
  }

  async function refreshRoles(): Promise<void> {
    // Two reads, because they answer two questions: the catalog is what may be *picked* and
    // never contains a retired row, while the editor's list is what exists — and a screen that
    // offers "retirar" while refusing to show what is retired offers a delete with manners.
    const [picker, editor] = await Promise.all([
      getCatalog(),
      getRoles({ status: "all" }),
    ]);
    if (picker.ok) {
      roles = picker.data.roles;
      renderRolePickers();
    }
    if (editor.ok) {
      editable = editor.data;
      renderRoleList();
      setRolesState(editable.length === 0 ? "empty" : "ready");
      return;
    }
    // A failure is not a slow success: leaving the loading arm up would spin
    // forever and say nothing, which is the same ambiguity this state exists to
    // remove. The arm goes quiet and `showRolesError` is the only thing left
    // speaking.
    setRolesState("error");
    showRolesError(editor.error);
  }

  function renderRoleList(): void {
    const list = find("[data-roles-list]");
    const source = find<HTMLTemplateElement>("[data-role-template]");
    if (list === null || source === null) return;

    const active = new Set(roles.map((role) => role.code));
    const fragment = document.createDocumentFragment();
    for (const role of editable) {
      const clone = source.content.firstElementChild?.cloneNode(true) ?? null;
      if (!(clone instanceof HTMLElement)) continue;
      clone.dataset.roleCode = role.code;
      // `TaxonomyRef` carries no `is_active`, so "retired" is derived by difference: this list
      // is everything, the catalog is what may be picked, and what is missing from the second
      // is what has been retired. One fact, read from the two answers that already exist.
      const isActive = active.has(role.code);
      clone.classList.toggle("is-retired", !isActive);
      const retire = clone.querySelector<HTMLElement>(
        '[data-field="role-retire"]',
      );
      if (retire !== null) retire.hidden = !isActive;
      const restore = clone.querySelector<HTMLElement>(
        '[data-field="role-restore"]',
      );
      if (restore !== null) restore.hidden = isActive;
      const name = clone.querySelector<HTMLInputElement>(
        '[data-field="role-label"]',
      );
      if (name !== null) {
        name.value = role.label;
        // The saved value, so "has this been edited?" is a comparison rather than a flag that
        // a second code path could forget to clear.
        name.dataset.saved = role.label;
      }
      const slug = clone.querySelector<HTMLElement>('[data-field="role-slug"]');
      if (slug !== null) slug.textContent = role.code;
      fragment.append(clone);
    }
    list.replaceChildren(fragment);
  }

  /**
   * Keeps the toolbar facet and the member form's picker showing the same vocabulary.
   *
   * Each keeps its own leading entry — "Todos" narrows nothing, "Sin rol" stores nothing — so the
   * blank option is read back off the control rather than named here, where one word would have
   * to serve two different questions. A role that was just retired is gone from the list, and
   * `setMenuOptions` falls the control back to that entry instead of keeping a value the server
   * would now reject.
   */
  function renderRolePickers(): void {
    const controls = [
      findMenuSelect(root, "role"),
      findMenuSelect(root, "member-role"),
    ];
    for (const control of controls) {
      if (control === null) continue;
      const blank = menuOptions(control).find((option) => option.value === "");
      setMenuOptions(control, [
        ...(blank === undefined ? [] : [blank]),
        ...roles.map((role) => ({ value: role.code, label: role.label })),
      ]);
    }
  }

  /**
   * Shows the save control for a role whose name has been edited, and hides it again the moment
   * the text matches what is stored.
   *
   * This is the whole interaction: no control until there is something to save, so the button's
   * presence *is* the explanation of what it does. The alternative — a permanent tick beside
   * every row — asks the reader to guess whether it saves, confirms or selects.
   */
  function syncRoleDirty(input: HTMLInputElement): void {
    const row = input.closest<HTMLElement>("[data-role-code]");
    const save = row?.querySelector<HTMLElement>('[data-field="role-save"]');
    if (save === null || save === undefined) return;
    save.hidden = input.value.trim() === (input.dataset.saved ?? "");
  }

  function showRolesError(error: ApiError | null): void {
    const slot = find('[data-field="roles-error"]');
    if (slot === null) return;
    if (error === null) {
      slot.textContent = "";
      slot.hidden = true;
      return;
    }
    slot.textContent = roleErrorCopy(error);
    slot.hidden = false;
  }

  async function submitRole(form: HTMLFormElement): Promise<void> {
    showRolesError(null);
    const data = new FormData(form);
    const submit = form.querySelector<HTMLButtonElement>(
      '[data-action="submit-role"]',
    );
    markBusy(submit);
    const result = await postRole({
      code: String(data.get("code") ?? "").trim(),
      label: String(data.get("label") ?? "").trim(),
    });
    clearBusy(submit);

    if (!result.ok) {
      showRolesError(result.error);
      return;
    }
    form.reset();
    toast({ kind: "success", title: `Rol "${result.data.label}" añadido` });
    await refreshRoles();
  }

  async function handleRoleAction(
    action: string,
    row: HTMLElement,
  ): Promise<void> {
    const code = row.dataset.roleCode;
    if (code === undefined) return;
    showRolesError(null);

    if (action === "rename") {
      const input = row.querySelector<HTMLInputElement>(
        '[data-field="role-label"]',
      );
      const label = input?.value.trim() ?? "";
      if (label === "") return;
      const renamed = await patchRole(code, { label });
      if (!renamed.ok) {
        showRolesError(renamed.error);
        return;
      }
      toast({
        kind: "success",
        title: `Rol renombrado a "${renamed.data.label}"`,
      });
      await refreshRoles();
      // The roster renders role *labels*, so a rename changes what every row says.
      void fetchAndRender();
      return;
    }

    if (action !== "toggle") return;

    const label =
      row.querySelector<HTMLInputElement>('[data-field="role-label"]')?.value ??
      code;
    const retiring = !row.classList.contains("is-retired");

    // Only the retirement is confirmed. Restoring is reversible by the same button and costs
    // nothing if it was a misclick, and a confirmation on every action is one nobody reads.
    if (retiring) {
      const confirmed = await confirmAction(
        `¿Retirar el rol "${label}"?`,
        "Desaparece de los selectores, así que no podrás asignárselo a nadie más. Quien ya lo tenga lo conserva y su ficha lo sigue mostrando; nada se borra.",
        "Retirar el rol",
      );
      if (!confirmed) return;
    }

    const result = await patchRole(code, { is_active: !retiring });
    if (!result.ok) {
      showRolesError(result.error);
      return;
    }
    toast({
      kind: "success",
      title: retiring
        ? `Rol "${result.data.label}" retirado de los selectores`
        : `Rol "${result.data.label}" de vuelta en los selectores`,
    });
    await refreshRoles();
  }

  /**
   * Asks in the product's own dialog and resolves with what the operator chose.
   *
   * A promise around the `close` event rather than `window.confirm`, which blocks the whole
   * page in browser chrome with English buttons and a `localhost:4321 says` title. `close`
   * fires however the dialog was dismissed — the button, `Escape`, the backdrop — so the
   * cancelling paths need no handler of their own and none of them can leak a pending promise.
   *
   * The dialog absent from the DOM resolves `false`: a confirmation that cannot be shown must
   * refuse the action, never assume it.
   */
  function confirmAction(
    title: string,
    body: string,
    cta: string,
  ): Promise<boolean> {
    const dialog = find<HTMLDialogElement>("[data-confirm-dialog]");
    if (dialog === null) return Promise.resolve(false);

    setDialogText(dialog, "confirm-title", title);
    setDialogText(dialog, "confirm-body", body);
    setDialogText(dialog, "confirm-cta", cta);
    dialog.returnValue = "";

    return new Promise<boolean>((resolve) => {
      dialog.addEventListener(
        "close",
        () => resolve(dialog.returnValue === "confirm"),
        { once: true },
      );
      dialog.showModal();
    });
  }

  function setDialogText(
    dialog: HTMLElement,
    field: string,
    text: string,
  ): void {
    const slot = dialog.querySelector<HTMLElement>(`[data-field="${field}"]`);
    if (slot !== null) slot.textContent = text;
  }

  // --- Wiring ----------------------------------------------------------------

  /** Any typing inside the roles dialog: the only field there is a role's name. */
  function onRoleInput(event: Event): void {
    const target = event.target;
    if (!(target instanceof HTMLInputElement)) return;
    if (target.dataset.field !== "role-label") return;
    syncRoleDirty(target);
  }

  /** Enter in a role name saves it, because that is what Enter means in a text field. */
  function onRoleKeydown(event: KeyboardEvent): void {
    if (event.key !== "Enter") return;
    const target = event.target;
    if (!(target instanceof HTMLInputElement)) return;
    if (target.dataset.field !== "role-label") return;
    event.preventDefault();
    const row = target.closest<HTMLElement>("[data-role-code]");
    if (row !== null) void handleRoleAction("rename", row);
  }

  function onToolbarInput(event: Event): void {
    const target = event.target;
    if (!(target instanceof HTMLInputElement)) return;
    if (searchTimer !== null) window.clearTimeout(searchTimer);
    const value = target.value;
    searchTimer = window.setTimeout(() => {
      searchTimer = null;
      applyQuery({ ...query, search: value });
    }, SEARCH_DEBOUNCE_MS);
  }

  /**
   * A choice in any picker on the surface: the three toolbar facets narrow the roster, the
   * dialog's role is only painted.
   *
   * The paint comes first and unconditionally, because a `MenuSelect` has no hidden input: its
   * `data-value` is the field, so a control left unpainted holds the previous answer while
   * displaying the new one.
   */
  function chooseMenu(control: HTMLElement, item: HTMLElement): void {
    applyMenuChoice(control, item);
    const value = item.dataset["value"] ?? "";

    if (control.dataset["facet"] === "role") {
      applyQuery({ ...query, role: value === "" ? null : value });
      return;
    }
    if (control.dataset["facet"] === "status") {
      // Narrowed by the same parser the URL goes through, so an unknown value falls back to the
      // default status here exactly as it would on a shared link.
      const next = queryFromParams(new URLSearchParams(`status=${value}`));
      applyQuery({ ...query, status: next.status });
      return;
    }
    if (control.dataset["facet"] !== "overloaded") return;
    applyQuery({
      ...query,
      overloaded: value === "" ? null : value === "true",
    });
  }

  function onClick(event: MouseEvent): void {
    const target = event.target;
    if (!(target instanceof Element)) return;

    const sort = target.closest<HTMLElement>("[data-sort]");
    if (sort !== null) {
      applyQuery(toggledSort(query, sort.dataset.sort as SortField));
      return;
    }

    const rosterAction = target.closest<HTMLElement>("[data-roster-action]");
    if (rosterAction !== null) {
      handleRosterAction(rosterAction.dataset.rosterAction ?? "");
      return;
    }

    const roleAction = target.closest<HTMLElement>("[data-role-action]");
    if (roleAction !== null) {
      const row = roleAction.closest<HTMLElement>("[data-role-code]");
      if (row !== null) {
        void handleRoleAction(roleAction.dataset.roleAction ?? "", row);
      }
      return;
    }

    const memberAction = target.closest<HTMLElement>("[data-member-action]");
    if (memberAction === null) return;
    const alias =
      memberAction.closest<HTMLElement>("[data-alias]")?.dataset.alias;
    if (alias === undefined) return;
    handleMemberAction(memberAction.dataset.memberAction ?? "", alias);
  }

  function handleRosterAction(action: string): void {
    if (action === "create") {
      openMemberDialog({ kind: "create" });
      return;
    }
    if (action === "roles") {
      showRolesError(null);
      // Reset to loading on every open: the previous answer belongs to the
      // previous open, and showing it while a new read is in flight would date
      // the list without saying so.
      setRolesState("loading");
      find<HTMLDialogElement>("[data-roles-dialog]")?.showModal();
      // Opened first, filled second: the dialog appears immediately and the list arrives, rather
      // than the button doing nothing visible while a request is in flight.
      void refreshRoles();
      return;
    }
    if (action === "clear") {
      resetToolbar();
      applyQuery(DEFAULT_QUERY);
      return;
    }
    if (action === "retry") {
      setArm("loading");
      void fetchAndRender();
      return;
    }
    if (action !== "reconnect") return;
    retry();
  }

  function handleMemberAction(action: string, alias: string): void {
    if (action === "edit") {
      openMemberDialog({ kind: "edit", alias });
      return;
    }
    if (action === "password") {
      passwordTarget = alias;
      const dialog = passwordDialog();
      const name = dialog?.querySelector<HTMLElement>(
        '[data-field="password-target"]',
      );
      if (name !== null && name !== undefined) {
        name.textContent = rowFor(alias)?.dataset.label ?? alias;
      }
      dialog?.querySelector<HTMLFormElement>("[data-password-form]")?.reset();
      dialog?.showModal();
      return;
    }
    if (action !== "toggle-active") return;
    void toggleActive(alias);
  }

  /** Puts the controls back to what the default query says, so the form and the URL agree. */
  function resetToolbar(): void {
    const toolbar = find<HTMLFormElement>("[data-roster-toolbar]");
    if (toolbar === null) return;
    const search = toolbar.elements.namedItem("q");
    if (search instanceof HTMLInputElement) search.value = "";
    // The pickers are not form controls, so `elements` does not carry them and a `reset()` would
    // not reach them: each is put back by writing its value.
    setFacet(toolbar, "role", "");
    setFacet(toolbar, "status", DEFAULT_QUERY.status);
    setFacet(toolbar, "overloaded", "");
  }

  /** Puts one picker back to `value`, if the surface renders it. */
  function setFacet(scope: ParentNode, facet: string, value: string): void {
    const control = findMenuSelect(scope, facet);
    if (control !== null) setMenuValue(control, value);
  }

  function onSubmit(event: SubmitEvent): void {
    const form = event.target;
    if (!(form instanceof HTMLFormElement)) return;
    if (!form.reportValidity()) {
      event.preventDefault();
      return;
    }
    event.preventDefault();
    if (form.matches("[data-member-form]")) {
      void submitMember(form);
      return;
    }
    if (form.matches("[data-password-form]")) {
      void submitPassword(form);
      return;
    }
    if (form.matches("[data-role-form]")) void submitRole(form);
  }

  function handleEnvelope(envelope: Envelope): void {
    // ISO-8601 UTC instants compare lexicographically, so the newest one wins without parsing.
    if (lastEventAt === null || envelope.occurred_at > lastEventAt) {
      lastEventAt = envelope.occurred_at;
    }
    scheduleRefresh();
  }

  /**
   * Follows the shared connection.
   *
   * `connecting` deliberately changes nothing: a reconnect in flight is still not live data,
   * and hiding the staleness marker the moment somebody presses "Reconectar" would promise a
   * recovery that has not happened yet.
   */
  function handleStatus(next: Status): void {
    status = next;
    if (next === "connecting") return;

    if (next === "disconnected") {
      missedEvents = true;
      if (root.dataset.view === "ready") setArm("disconnected");
      renderStale(lastEventAt);
      return;
    }

    if (root.dataset.view === "disconnected") setArm("ready");
    // The transport cannot replay what it missed (Redis pub/sub is live-only), so the end of
    // an outage — and only that — is the moment to ask the server what changed meanwhile.
    if (!missedEvents) return;
    missedEvents = false;
    void fetchAndRender();
  }

  // --- Mount -----------------------------------------------------------------

  for (const actions of root.querySelectorAll<HTMLElement>(
    "[data-roster-actions]",
  )) {
    actions.hidden = !isOpsLead;
  }

  root.addEventListener("click", onClick);
  root.addEventListener("submit", onSubmit);
  root.addEventListener("input", onRoleInput);
  root.addEventListener("keydown", onRoleKeydown);
  const toolbar = find("[data-roster-toolbar]");
  toolbar?.addEventListener("input", onToolbarInput);
  // One popup grammar for every picker on the surface — the three facets and the member form's
  // role — mounted on the region so a dialog opened later is operable without a second mount.
  cleanups.push(
    mountMenus(root, "[data-menu-select]", {
      fill: () => Promise.resolve(),
      choose: (control, item) => {
        chooseMenu(control, item);
        return Promise.resolve();
      },
    }),
  );

  for (const dialog of root.querySelectorAll<HTMLDialogElement>("dialog")) {
    dialog.addEventListener("click", (event) => {
      const target = event.target;
      if (!(target instanceof Element)) return;
      if (target.closest('[data-action^="close-"]') === null) return;
      dialog.close();
    });
  }

  cleanups.push(onStatus(handleStatus));
  cleanups.push(
    onReset(() => {
      void fetchAndRender();
    }),
  );
  for (const topic of WATCHED_TOPICS) {
    cleanups.push(subscribe(topic, handleEnvelope));
  }

  // The server render carries no session — the credential lives in localStorage, which SSR
  // cannot read — so a first paint that failed is retried here, where the token exists.
  if (root.dataset.view === "error") {
    setArm("loading");
    void fetchAndRender();
  }

  return () => {
    for (const off of cleanups) off();
    cleanups.length = 0;
    root.removeEventListener("click", onClick);
    root.removeEventListener("submit", onSubmit);
    root.removeEventListener("input", onRoleInput);
    root.removeEventListener("keydown", onRoleKeydown);
    toolbar?.removeEventListener("input", onToolbarInput);
    if (refreshTimer !== null) window.clearTimeout(refreshTimer);
    if (searchTimer !== null) window.clearTimeout(searchTimer);
    // Invalidates any response still in flight: it must not render into the next page.
    requestSeq += 1;
  };
}

/**
 * The roles the server already rendered into the toolbar's facet.
 *
 * Read out of the DOM rather than fetched on mount: the page's frontmatter has just asked for
 * the catalog, and a second request for a list already on screen would be a round trip whose
 * only possible outcome is the same answer.
 */
function readRenderedRoles(root: HTMLElement): readonly TaxonomyRef[] {
  const toolbar = root.querySelector<HTMLElement>("[data-roster-toolbar]");
  const control = toolbar === null ? null : findMenuSelect(toolbar, "role");
  if (control === null) return [];
  // The leading "Todos" is the absence of a filter, not a role somebody can hold.
  return menuOptions(control)
    .filter((option) => option.value !== "")
    .map((option) => ({
      code: option.value,
      label: option.label,
      color: null,
    }));
}

/**
 * Spanish for a failed role write, in this dialog's own words.
 *
 * The shared `errorCopy` answers `conflicting_state` with "reload to see the current state",
 * which is true of a blocker somebody else resolved and useless here: the code is taken, the
 * operator has to choose another, and the row they collided with may be one they cannot even see
 * because it is retired. A screen that knows what its conflict means says so.
 */
function roleErrorCopy(error: ApiError): string {
  if (error.kind === "validation") {
    return Object.values(error.fields).flat().join(" ");
  }
  if (error.code === "conflicting_state") {
    return "Ya existe un rol con ese código, aunque esté retirado. Elige otro código, o devuelve el que existe a los selectores.";
  }
  if (error.kind === "permission_denied") {
    return "Sólo un responsable de operación puede cambiar los roles.";
  }
  return errorCopy(error.code);
}

/** An empty password field means "no credential", which is a value and not a missing one. */
function passwordOrNull(value: FormDataEntryValue | null): string | null {
  const password = String(value ?? "");
  return password === "" ? null : password;
}

function markBusy(button: HTMLButtonElement | null): void {
  if (button === null) return;
  button.classList.add("is-loading");
  button.disabled = true;
}

function clearBusy(button: HTMLButtonElement | null): void {
  if (button === null) return;
  button.classList.remove("is-loading");
  button.disabled = false;
}
