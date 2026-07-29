"""Routes of the portfolio context: the queue, project read and write, and team load.

Every handler here is the same four lines: read the actor, build a command, call one service,
return what it gave back. There is no ``try/except`` — :mod:`config.errors` maps every typed
rejection once — and no ``models`` import, so a route cannot quietly grow a query.

``now`` is read once per request and threaded into the service. Services never call
``timezone.now()`` themselves: that is what makes a replay deterministic and lets a test assert on
an exact instant. ``correlation_id`` is minted here for the same reason it exists — every record
and event a single request produces shares it, so "deprioritize A in order to prioritize B"
reconstructs as one decision instead of two unrelated rows.
"""

from uuid import uuid4

from django.http import HttpRequest
from django.utils import timezone
from ninja import Query, Router
from ninja.responses import Status

from apps.portfolio.api.schemas import (
    ProjectCreateIn,
    ProjectUpdateIn,
    QueueQuery,
    TeamLoadQuery,
    TransitionIn,
)
from apps.portfolio.domain.value_objects import (
    CreateProjectCommand,
    RosterFilters,
    SnapshotQueueFilters,
    UpdateProjectCommand,
)
from apps.portfolio.domain.views import (
    ClientDirectoryView,
    ProjectDetailView,
    QueueItemView,
    TeamLoadPage,
)
from apps.portfolio.services import (
    assign_project_workflow,
    create_project,
    read_clients,
    read_project_detail,
    read_queue,
    read_team_load,
    transition_project,
    update_project,
)
from apps.shared.pagination import Page, PageWindow
from apps.workflow.api.schemas import WorkflowAssignmentIn
from config.auth import actor_code_of

router = Router(tags=["portfolio"])


@router.get("/queue", response=Page[QueueItemView], url_name="queue")
def get_queue(request: HttpRequest, filters: Query[QueueQuery]) -> Page[QueueItemView]:
    """The prioritized project queue: score, breakdown and risk flags, in one read.

    Served from ``ProjectSnapshot`` — the whole command center in one index scan rather than a
    six-table join with per-row aggregates (ARCHITECTURE §8). The flags and the health beside each
    row are evaluated from that row at request time (ADR 0011), so the queue is correct the moment
    it is loaded rather than the moment it was last rebuilt.
    """
    del request
    window = PageWindow.of(page=filters.page, page_size=filters.page_size)
    return read_queue(
        SnapshotQueueFilters(
            health=filters.health,
            state_category=filters.state_category,
            state_code=filters.state,
            engagement_type_codes=tuple(filters.engagement_type),
            project_type_code=filters.project_type,
            stage_code=filters.stage,
            owner_codes=tuple(filters.owner),
            risk_flag_codes=tuple(filters.risk_flag),
            has_open_blockers=filters.has_open_blockers,
            is_archived=filters.is_archived,
            search=filters.q,
            order_by=filters.order_by,
            limit=window.limit,
            offset=window.offset,
        ),
        now=timezone.now(),
    )


@router.get("/clients", response=ClientDirectoryView, url_name="clients")
def get_clients(request: HttpRequest) -> ClientDirectoryView:
    """The counterparties a project can be registered against.

    Here and not on ``GET /api/v1/catalog`` because ``Client`` is a portfolio aggregate, not one of
    the operator-editable taxonomies that document publishes: serving it there would make the
    catalog context read a model another context owns.

    Retired counterparties are absent — the projects already pointing at one keep resolving through
    their foreign key, but nobody can pick it again.
    """
    del request
    return read_clients()


@router.get(
    "/projects/{project_code}",
    response=ProjectDetailView,
    url_name="project_detail",
)
def get_project(request: HttpRequest, project_code: str) -> ProjectDetailView:
    """One project with its tasks, blockers, risk flags and **legal transitions**.

    ``transitions`` is the only source of transition buttons: the frontend renders exactly what
    arrives and never guesses legality, so adding a workflow state is a fixture row.
    """
    del request
    return read_project_detail(project_code=project_code, now=timezone.now())


@router.post(
    "/projects",
    response={201: ProjectDetailView},
    url_name="project_create",
)
def post_project(request: HttpRequest, payload: ProjectCreateIn) -> Status[ProjectDetailView]:
    """Register a project in the initial state of the workflow its engagement type binds to.

    Neither ``code`` nor ``workflow_state`` is accepted: the first is allocated by the service, the
    second is resolved from ``WorkflowBinding`` (CLAUDE.md rule 2).
    """
    now = timezone.now()
    created = create_project(
        CreateProjectCommand(
            name=payload.name,
            client_code=payload.client,
            engagement_type_code=payload.engagement_type,
            project_type_code=payload.project_type,
            stage_code=payload.stage,
            owner_code=payload.owner,
            start_date=payload.start_date,
            target_date=payload.target_date,
            business_value=payload.business_value,
            currency_code=payload.currency,
            summary=payload.summary,
            description=payload.description,
            next_step=payload.next_step,
        ),
        actor=actor_code_of(request),
        correlation_id=uuid4(),
        now=now,
    )
    return Status(201, read_project_detail(project_code=created.code, now=now))


@router.patch(
    "/projects/{project_code}",
    response=ProjectDetailView,
    url_name="project_update",
)
def patch_project(
    request: HttpRequest, project_code: str, payload: ProjectUpdateIn
) -> ProjectDetailView:
    """Edit a project's mutable fields. Absent means untouched; explicit ``null`` clears.

    ``workflow_state``, ``health`` and ``score`` are not fields of this payload at all: a state
    moves through the transition route, and the other two are derived.
    """
    now = timezone.now()
    update_project(
        _update_command(project_code=project_code, payload=payload),
        actor=actor_code_of(request),
        correlation_id=uuid4(),
        now=now,
    )
    return read_project_detail(project_code=project_code, now=now)


@router.post(
    "/projects/{project_code}/transition",
    response=ProjectDetailView,
    url_name="project_transition",
)
def post_project_transition(
    request: HttpRequest, project_code: str, payload: TransitionIn
) -> ProjectDetailView:
    """Move a project along a declared edge, returning it with its **new** legal transitions.

    An undeclared or inactive edge is ``409 transition_not_allowed`` carrying the moves that *are*
    legal, so a client whose button list went stale resyncs from the rejection.
    """
    now = timezone.now()
    transition_project(
        project_code=project_code,
        to_state_code=payload.to_state,
        actor=actor_code_of(request),
        reason=payload.reason,
        correlation_id=uuid4(),
        now=now,
    )
    return read_project_detail(project_code=project_code, now=now)


@router.put(
    "/projects/{project_code}/workflow",
    response=ProjectDetailView,
    url_name="project_workflow_assign",
)
def put_project_workflow(
    request: HttpRequest, project_code: str, payload: WorkflowAssignmentIn
) -> ProjectDetailView:
    """Put this project on a named lifecycle, overriding what its engagement type binds.

    A route of its own rather than a field of ``PATCH /projects/{code}`` for two reasons that
    survive the permission being the same as every other project write: it can be refused for a
    cause no other field of a project edit has — the target graph not containing the state the
    project stands on — and a service that both edited scalars and reassigned lifecycles would be
    two use cases sharing one transaction boundary.

    **Any member, deliberately.** This was an ops-lead write, on the argument that deciding which
    lifecycle a record obeys is a lead's call. What made that untenable is that the same act reaches
    the operation by a second door: changing a project's engagement type through
    ``PATCH /projects/{code}`` re-resolves the binding and therefore the graph, and that field is
    any member's. One act behind two doors, one locked and one open, is not a permission model —
    it is a lock on the door nobody was using. Closing the other door instead would take the
    engagement type away from the people who own it, so the gate came off this one.

    **It changes the graph, never the state.** The project keeps the state ``code`` it was standing
    on and is repointed at that state in the target graph, so this cannot be used to move a project
    to a state no edge leads to. Moving remains ``POST /projects/{code}/transition``.

    ``409 conflicting_state`` when the target has no active state carrying the project's current
    state code, with ``details.available`` listing the states it does offer: either add the missing
    column to the target (§2.19) or move the project first, then reassign.

    Errors: ``404 not_found`` (no such project, or no such workflow), ``409 conflicting_state``
    (incompatible state; or the target is retired), ``422 validation_error`` (the target governs
    tasks, not projects).
    """
    now = timezone.now()
    assign_project_workflow(
        project_code=project_code,
        workflow_code=payload.workflow,
        actor=actor_code_of(request),
        correlation_id=uuid4(),
        now=now,
    )
    return read_project_detail(project_code=project_code, now=now)


@router.delete(
    "/projects/{project_code}/workflow",
    response=ProjectDetailView,
    url_name="project_workflow_clear",
)
def delete_project_workflow(request: HttpRequest, project_code: str) -> ProjectDetailView:
    """Stop this project following its own lifecycle: it inherits one again.

    ``DELETE`` removes the *assignment*, never a workflow and never the project. What comes back is
    the project following whatever the binding ladder hands it — its engagement type's binding, the
    per-kind default binding, or the default graph — and the detail's ``workflow.source`` flips from
    ``DIRECT`` to ``INHERITED``.

    The inherited graph is compatibility-checked exactly like a named one: if it does not contain
    the state the project is standing on, this is the same ``409 conflicting_state``. "Inherited" is
    not a synonym for "safe", and a project cannot be dropped back onto a lifecycle that has no
    column for where it stands.

    Errors: ``404 not_found``, ``409 conflicting_state``, ``422 validation_error`` (no binding and
    no default workflow answers for projects).
    """
    now = timezone.now()
    assign_project_workflow(
        project_code=project_code,
        workflow_code=None,
        actor=actor_code_of(request),
        correlation_id=uuid4(),
        now=now,
    )
    return read_project_detail(project_code=project_code, now=now)


@router.get("/team/load", response=TeamLoadPage, url_name="team_load")
def get_team_load(request: HttpRequest, filters: Query[TeamLoadQuery]) -> TeamLoadPage:
    """The roster: load per person, computed from task rows at read time.

    The source ``Team`` sheet's counters are a stale projection of the same rows and are
    deliberately not imported: a stored count and the tasks it summarises drift apart in silence.

    Reading who is on the team is everybody's business; changing it is not. The writes are
    ``POST``/``PATCH``/``DELETE /team/members`` in the identity context, behind an ops-lead check —
    the roster row is ``accounts.User``, and only its *load* belongs to the portfolio.

    ``order_by`` outside the allowlist is ``422 validation_error`` carrying what may be sorted by.
    """
    del request
    return TeamLoadPage(
        items=read_team_load(
            RosterFilters(
                owner_codes=tuple(filters.owner),
                role_codes=tuple(filters.role),
                status=filters.status,
                overloaded=filters.overloaded,
                search=filters.q,
                order_by=filters.order_by,
            ),
            as_of=timezone.localdate(),
        )
    )


def _update_command(*, project_code: str, payload: ProjectUpdateIn) -> UpdateProjectCommand:
    """Carry the caller's absent-versus-null distinction from the payload into the command.

    Only the fields the client actually sent are copied. Building the command from every attribute
    would turn "do not touch the target date" into "clear the target date", because both arrive as
    ``None`` — which is precisely the bug ``model_fields_set`` exists to prevent, and why this is a
    translation rather than a ``model_dump()``.
    """
    renamed = {
        "client": "client_code",
        "engagement_type": "engagement_type_code",
        "project_type": "project_type_code",
        "stage": "stage_code",
        "owner": "owner_code",
    }
    fields = {
        renamed.get(name, name): value
        for name, value in payload.model_dump(exclude_unset=True).items()
    }
    return UpdateProjectCommand(code=project_code, **fields)
