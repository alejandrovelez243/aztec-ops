"""Routes of the workflow context: reading every state graph, and shaping them.

Reading is everybody's business — a board cannot draw a column it was not told about. Writing is an
ops lead's, on every route without exception, because authoring a lifecycle decides how everyone
else's work is allowed to behave: which columns exist, which moves are offered, which of them demand
a written reason. It is the same rule as the priority override and the portfolio recompute
(``User.is_ops_lead``), declared per route so the OpenAPI document says so and no endpoint can join
the protected set by being forgotten.

**Authoring a graph and obeying one stay separate.** Nothing in this module is read while a record
moves: ``validate_transition`` re-reads the rows every time it decides, so an edge declared here
becomes *available*, never *taken*, and a client that acts on one without asking the record still
meets the same typed 409. That separation is why these writes can exist at all.

Every write answers with the whole graph, in exactly the shape ``GET /workflows`` publishes. An
editor that added a state gets back the arrangement including the ``order`` it did not send, the
occupancy that decides which nodes may now be retired, and the arrows a retirement withdrew — none
of it derivable from an echo of the request it sent.
"""

from uuid import uuid4

from django.http import HttpRequest
from django.utils import timezone
from ninja import Router
from ninja.responses import Status

from apps.workflow.api.schemas import (
    StateCreateIn,
    StateUpdateIn,
    TransitionCreateIn,
    TransitionUpdateIn,
    WorkflowCreateIn,
    WorkflowUpdateIn,
)
from apps.workflow.domain.commands import (
    AddStateCommand,
    AddTransitionCommand,
    CreateWorkflowCommand,
    UpdateStateCommand,
    UpdateTransitionCommand,
    UpdateWorkflowCommand,
)
from apps.workflow.domain.guards import registered_guard_codes
from apps.workflow.domain.views import WorkflowCatalogView, WorkflowShapeView
from apps.workflow.services import (
    add_state,
    add_transition,
    create_workflow,
    read_workflows,
    retire_state,
    retire_transition,
    update_state,
    update_transition,
    update_workflow,
)
from config.auth import actor_code_of, workflow_author

router = Router(tags=["workflow"])


@router.get("/workflows", response=WorkflowCatalogView, url_name="workflows_read")
def get_workflows(request: HttpRequest) -> WorkflowCatalogView:
    """Return every configured state graph: its states and its edges, as an operator arranged them.

    Each state carries ``code``, ``label``, ``category`` and ``color`` — the shared
    :class:`~apps.shared.refs.StateRef` every read surface already renders — plus what an *editor*
    needs and cannot derive: ``order``, ``is_initial``, ``is_terminal``, ``is_active``,
    ``record_count`` and ``can_retire``. Each graph carries the engagement types bound to it, the
    kind of aggregate it governs, whether it is the fallback for that kind, and ``transitions``:
    every active edge as ``from_state`` / ``to_state`` codes plus the operator's ``label``,
    ``requires_reason`` and ``requires_fields``.

    Authenticated like every other read; the list of routes that opt out is five long and closed
    (`docs/API.md` §1.2). The *writes* below additionally require an ops lead — reading the shape of
    the operation is not the same permission as deciding it.

    One document rather than a route per engagement type, for the same reason ``GET /catalog`` is
    one document: a board draws all of its columns at once. Asking per engagement type would also
    force the client to know which type to ask about before its first request, which means
    reimplementing the binding precedence ``Workflow.objects.resolve`` owns.

    **Why publishing edges here does not reopen the decision that kept them out.** This route
    deliberately shipped without edges so that a client could not compute legality locally and skip
    the per-project and per-task ``transitions`` list. That rule stands, unchanged and unweakened.
    What makes edges safe on *this* document is a real distinction, not a rhetorical one: this
    document describes the graph **as an operator configured it**, while a project's or a task's own
    ``transitions`` says what **that record** may do **right now**. The two can legitimately
    disagree, and the ways they disagree are the whole argument. A configured edge is refused when
    the record is not sitting on its ``from_state``; when a guard rejects it — ``GuardRejected`` is
    in the error mapping (§1.5) precisely because a declared edge can be denied; and when a field
    named in ``requires_fields`` is empty *on that record*, which is a fact about a row, not about
    the graph. **So an edge existing is not a move being legal.** A client that reads an arrow here
    and acts on it without asking the record meets exactly the same typed 409 it met before, now
    carrying ``details.allowed``. Every enforcement path stays where it is: nothing in this route is
    read by ``validate_transition``, which re-reads the rows each time it decides. The same holds
    for the authoring fields: ``can_retire`` answers whether an *operator* may remove a column, never
    whether a *record* may move.

    **Nor does it become the global state list §2.17 refuses.** ``GET /catalog`` excludes states
    because a state code is unique only inside its workflow, so a *flat* list invites the client to
    guess. Nothing here is flat: states arrive grouped under the graph that owns them, and each edge
    names its endpoints by code inside that same graph, so ``bloqueada`` in two workflows still
    cannot be confused. A board needs the columns and cannot derive them — columns inferred from the
    states projects happen to occupy cannot represent an empty one, so a workflow whose ``Bloqueado``
    state is unoccupied has no such column, cannot say "nothing is blocked", and cannot accept a card
    dropped into it — and a screen that draws the lifecycle needs the arrows and cannot derive those
    either, since an unused edge is invisible to every record that never took it.

    The ``guard`` a move names is still **not** published, on any of these shapes. Whether a guard
    passes depends on facts no row of the graph holds, so naming it would invite a client to predict
    an answer it cannot compute; the authoring surface accepts it as input and
    ``GET /workflows/guards`` lists the codes that exist.
    """
    del request
    return read_workflows()


@router.get(
    "/workflows/guards",
    response=list[str],
    auth=workflow_author,
    url_name="workflow_guards",
)
def get_workflow_guards(request: HttpRequest) -> list[str]:
    """Every guard code a transition may name, sorted — the picker the transition editor renders.

    Served from the registry rather than from a list in the frontend, which is what keeps CLAUDE.md
    rule 8 true end to end: adding a guard is one function plus one decorator line, and the picker
    follows with no second edit anywhere.

    Ops lead, like the writes it accompanies. A guard code is not something a reader of the board
    needs, and it is the one part of a move whose outcome nothing may predict — which is exactly why
    it appears here, on the authoring surface, and on no read of the graph.
    """
    del request
    return list(registered_guard_codes())


@router.post(
    "/workflows",
    response={201: WorkflowShapeView},
    auth=workflow_author,
    url_name="workflow_create",
)
def post_workflow(request: HttpRequest, payload: WorkflowCreateIn) -> Status[WorkflowShapeView]:
    """Create a lifecycle: an empty, active graph governing projects or tasks.

    It arrives empty and is shaped by the routes below, one node and one edge at a time. A lifecycle
    authored in a single request either succeeds whole or leaves an operator re-typing a form.

    ``engagement_types`` binds the graph so work of those types resolves to it. An engagement type
    already bound to another graph for the same entity kind is ``409 conflicting_state``: resolution
    has to be deterministic, so a type names exactly one lifecycle per kind.

    Errors: ``409 conflicting_state`` (code taken, or engagement type already bound),
    ``422 validation_error`` (``applies_to`` outside ``PROJECT`` | ``TASK``, unknown engagement
    type), ``403 permission_denied``.
    """
    return Status(
        201,
        create_workflow(
            CreateWorkflowCommand(
                code=payload.code,
                name=payload.name,
                applies_to=payload.applies_to,
                engagement_types=tuple(payload.engagement_types),
            ),
            actor=actor_code_of(request),
            correlation_id=uuid4(),
            now=timezone.now(),
        ),
    )


@router.patch(
    "/workflows/{workflow_code}",
    response=WorkflowShapeView,
    auth=workflow_author,
    url_name="workflow_update",
)
def patch_workflow(
    request: HttpRequest, workflow_code: str, payload: WorkflowUpdateIn
) -> WorkflowShapeView:
    """Rename a lifecycle, retire it, or put it back in service. Absent means untouched.

    Retiring is ``is_active = false`` and never a delete: the graph stops being offered to new work
    and keeps resolving for everything already inside it, whose states are ``PROTECT``ed exactly so.

    ``code`` and ``applies_to`` are not writable. The first is the address every binding and every
    published document names it by; the second would leave the records already in the graph governed
    by a lifecycle claiming to govern something else.

    Errors: ``404 not_found``, ``422 validation_error``, ``403 permission_denied``.
    """
    fields = payload.model_dump(exclude_unset=True)
    return update_workflow(
        UpdateWorkflowCommand(code=workflow_code, **fields),
        actor=actor_code_of(request),
        correlation_id=uuid4(),
        now=timezone.now(),
    )


@router.post(
    "/workflows/{workflow_code}/states",
    response={201: WorkflowShapeView},
    auth=workflow_author,
    url_name="workflow_state_create",
)
def post_workflow_state(
    request: HttpRequest, workflow_code: str, payload: StateCreateIn
) -> Status[WorkflowShapeView]:
    """Add a column to a lifecycle, appended unless ``order`` says where it goes.

    ``order`` omitted **appends**: a new column belongs at the end of an arrangement somebody already
    made, and landing at zero would silently reshuffle the board.

    The first state of an empty graph becomes its entry node — otherwise a graph authored entirely
    through the product could hold no new work until somebody opened the admin.

    ``category`` must be one of ``BACKLOG``, ``IN_PROGRESS``, ``BLOCKED``, ``DONE``, ``CANCELLED``:
    it is what every risk rule and every priority signal branches on, so a sixth value would be an
    inert state rather than a new behaviour.

    Errors: ``404 not_found`` (no such workflow), ``409 conflicting_state`` (the graph already has
    that state code, retired or not), ``422 validation_error`` (unknown category),
    ``403 permission_denied``.
    """
    return Status(
        201,
        add_state(
            AddStateCommand(
                workflow_code=workflow_code,
                code=payload.code,
                label=payload.label,
                category=payload.category,
                color=payload.color,
                order=payload.order,
            ),
            actor=actor_code_of(request),
            correlation_id=uuid4(),
            now=timezone.now(),
        ),
    )


@router.patch(
    "/workflows/{workflow_code}/states/{state_code}",
    response=WorkflowShapeView,
    auth=workflow_author,
    url_name="workflow_state_update",
)
def patch_workflow_state(
    request: HttpRequest, workflow_code: str, state_code: str, payload: StateUpdateIn
) -> WorkflowShapeView:
    """Reword, recolour, recategorise or move a column. Absent means untouched.

    ``code`` is not writable: every project and task on this node holds it, and renaming is what
    ``label`` is for. ``is_active`` accepts only ``true``, which restores a retired node — retiring
    is ``DELETE``, because it can be refused by records this request knows nothing about.

    Errors: ``404 not_found`` (no such workflow, or the graph has no such state — which is also the
    answer when the code names a state of a *different* graph), ``422 validation_error`` (unknown
    category, or ``is_active: false``), ``403 permission_denied``.
    """
    fields = payload.model_dump(exclude_unset=True)
    return update_state(
        UpdateStateCommand(workflow_code=workflow_code, code=state_code, **fields),
        actor=actor_code_of(request),
        correlation_id=uuid4(),
        now=timezone.now(),
    )


@router.delete(
    "/workflows/{workflow_code}/states/{state_code}",
    response=WorkflowShapeView,
    auth=workflow_author,
    url_name="workflow_state_retire",
)
def delete_workflow_state(
    request: HttpRequest, workflow_code: str, state_code: str
) -> WorkflowShapeView:
    """Take a column out of a lifecycle: this **retires** it and never deletes it.

    The row survives with ``is_active: false``, because history points at it and because the
    database ``PROTECT``s it. What the operator gets is the effect they asked for: the node leaves
    the graph, and every arrow touching it — in or out — is withdrawn in the same transaction, since
    a move to a column outside the graph is a move nothing may take.

    **Refused while records occupy it**, with ``409 conflicting_state`` carrying how many projects
    and how many tasks are standing there. That is a rule the database cannot state: a record parked
    on a retired node would have no column to be drawn in and no move to make, so the operator moves
    them first. The refusal names the numbers because "that failed" is not something anyone can act
    on.

    Withdrawing the last move *out of* a state is the opposite case and is allowed — that is how a
    terminal state is declared. It is ``DELETE`` on the transition, below.

    Errors: ``404 not_found``, ``409 conflicting_state`` (records are on the state),
    ``403 permission_denied``.
    """
    return retire_state(
        workflow_code=workflow_code,
        state_code=state_code,
        actor=actor_code_of(request),
        correlation_id=uuid4(),
        now=timezone.now(),
    )


@router.post(
    "/workflows/{workflow_code}/transitions",
    response={201: WorkflowShapeView},
    auth=workflow_author,
    url_name="workflow_transition_create",
)
def post_workflow_transition(
    request: HttpRequest, workflow_code: str, payload: TransitionCreateIn
) -> Status[WorkflowShapeView]:
    """Declare a move between two columns **of this graph**.

    Both endpoints are resolved inside the workflow in the path, which is what makes an edge across
    two lifecycles impossible to express rather than merely rejected: a state code is unique only
    inside its graph.

    ``guard``, when given, must be one of the codes ``GET /workflows/guards`` lists. Checked here
    rather than only when a record moves, so a typo cannot sit in the graph claiming to enforce a
    safety check that evaporated.

    Declaring an edge changes no record. The move becomes available from the state it leaves;
    whether a given project or task may take it is still decided per record, against the row it sits
    on, the fields filled in on it and the guard.

    Errors: ``404 not_found`` (no such workflow; or an endpoint is not a state of this graph),
    ``409 conflicting_state`` (this ordered pair already has an edge — restore it with ``PATCH``),
    ``422 validation_error`` (unregistered guard), ``403 permission_denied``.
    """
    return Status(
        201,
        add_transition(
            AddTransitionCommand(
                workflow_code=workflow_code,
                from_state=payload.from_state,
                to_state=payload.to_state,
                label=payload.label,
                requires_reason=payload.requires_reason,
                requires_fields=tuple(payload.requires_fields),
                guard=payload.guard,
                order=payload.order,
            ),
            actor=actor_code_of(request),
            correlation_id=uuid4(),
            now=timezone.now(),
        ),
    )


@router.patch(
    "/workflows/{workflow_code}/transitions/{from_state_code}/{to_state_code}",
    response=WorkflowShapeView,
    auth=workflow_author,
    url_name="workflow_transition_update",
)
def patch_workflow_transition(
    request: HttpRequest,
    workflow_code: str,
    from_state_code: str,
    to_state_code: str,
    payload: TransitionUpdateIn,
) -> WorkflowShapeView:
    """Change what a declared move asks for, or restore a withdrawn one. Absent means untouched.

    The ordered pair is the address and is not editable: an edge *is* its endpoints, so repointing
    an arrow is withdrawing one move and declaring another — two decisions, two lines in the trail.

    ``requires_fields`` is sent whole. ``is_active`` accepts only ``true``, and re-enabling this row
    is the *only* way a withdrawn move comes back: a second row for the same pair is forbidden by
    constraint, so ``POST`` answers ``409``.

    Errors: ``404 not_found`` (no such workflow or no such edge), ``422 validation_error``
    (unregistered guard, or ``is_active: false``), ``403 permission_denied``.
    """
    fields = payload.model_dump(exclude_unset=True)
    return update_transition(
        UpdateTransitionCommand(
            workflow_code=workflow_code,
            from_state=from_state_code,
            to_state=to_state_code,
            **fields,
        ),
        actor=actor_code_of(request),
        correlation_id=uuid4(),
        now=timezone.now(),
    )


@router.delete(
    "/workflows/{workflow_code}/transitions/{from_state_code}/{to_state_code}",
    response=WorkflowShapeView,
    auth=workflow_author,
    url_name="workflow_transition_retire",
)
def delete_workflow_transition(
    request: HttpRequest, workflow_code: str, from_state_code: str, to_state_code: str
) -> WorkflowShapeView:
    """Withdraw a declared move: this **retires** it and never deletes it.

    The row survives with ``is_active: false`` — the trail and the events name this edge by its
    endpoints, and deleting the row would turn a readable audit into two codes nothing resolves —
    and it disappears from ``transitions`` on the next read, because a withdrawn move is not a
    drawable arrow.

    **Withdrawing the last move out of a state is allowed**, and is how a terminal state is
    declared: nothing leaves ``entregado``, so ``entregado`` is where the lifecycle ends. No record
    is harmed by losing a move it had not taken — a record on that state simply has no buttons,
    which is what "terminal" means.

    Errors: ``404 not_found``, ``403 permission_denied``.
    """
    return retire_transition(
        workflow_code=workflow_code,
        from_state=from_state_code,
        to_state=to_state_code,
        actor=actor_code_of(request),
        correlation_id=uuid4(),
        now=timezone.now(),
    )
