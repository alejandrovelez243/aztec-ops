"""Routes of the ``work`` context: tasks, blockers and notes.

No ``try/except`` and no ``models`` import. Every write goes through one use case, every read
through one read service, and the mapping from a typed rejection to a status code lives once in
:mod:`config.errors` — an illegal task transition returns the documented 409, not a 500.

``now`` and ``correlation_id`` are established once per request and threaded down. A service that
read the clock itself could not be replayed, and a request that minted a fresh correlation id per
record would break the one link that lets a chained decision be reconstructed.
"""

from uuid import uuid4

from django.http import HttpRequest
from django.utils import timezone
from ninja import Query, Router
from ninja.responses import Status

from apps.shared.pagination import Page, PageWindow
from apps.work.api.schemas import (
    BlockerCreateIn,
    BlockerResolveIn,
    NoteIn,
    TaskCreateIn,
    TaskQuery,
    TaskTransitionIn,
)
from apps.work.domain.commands import (
    AddNoteCommand,
    CreateTaskCommand,
    DependencySpec,
    RaiseBlockerCommand,
    ResolveBlockerCommand,
    TransitionTaskCommand,
)
from apps.work.domain.views import BlockerView, NoteView, TaskView
from apps.work.services.add_note import add_note
from apps.work.services.create_task import create_task
from apps.work.services.raise_blocker import raise_blocker
from apps.work.services.read_tasks import TaskFilters, read_project_tasks
from apps.work.services.resolve_blocker import resolve_blocker
from apps.work.services.transition_task import transition_task
from config.actor import actor_code_of, actor_header

router = Router(tags=["work"])


@router.get(
    "/projects/{project_code}/tasks",
    response=Page[TaskView],
    auth=None,
    url_name="project_tasks",
)
def get_project_tasks(
    request: HttpRequest, project_code: str, filters: Query[TaskQuery]
) -> Page[TaskView]:
    """One page of a project's tasks, ordered for triage unless the caller says otherwise.

    Default order is severity descending then soonest due — the order an operator actually works
    in. ``priority`` sorts by ``Priority.weight``, so a priority inserted above ``critica`` from
    the admin sorts correctly with no code change.
    """
    del request
    window = PageWindow.of(page=filters.page, page_size=filters.page_size)
    return read_project_tasks(
        TaskFilters(
            project_code=project_code,
            assignee_codes=tuple(filters.assignee),
            priority_codes=tuple(filters.priority),
            state_code=filters.state,
            state_category=filters.state_category,
            is_overdue=filters.is_overdue,
            search=filters.q,
            order_by=filters.order_by,
            limit=window.limit,
            offset=window.offset,
        ),
        as_of=timezone.localdate(),
    )


@router.post(
    "/projects/{project_code}/tasks",
    response={201: TaskView},
    auth=actor_header,
    url_name="task_create",
)
def post_project_task(
    request: HttpRequest, project_code: str, payload: TaskCreateIn
) -> Status[TaskView]:
    """Add a task to a project, in the initial state of the task workflow.

    A dependency that would close a loop in the project's graph is refused before any edge is
    written, so a rejected cycle leaves no task with half its prerequisites.
    """
    now = timezone.now()
    task = create_task(
        CreateTaskCommand(
            project_code=project_code,
            title=payload.title,
            priority_code=payload.priority,
            assignee_code=payload.assignee,
            due_date=payload.due_date,
            detail=payload.detail,
            last_progress=payload.last_progress,
            dependencies=tuple(DependencySpec(depends_on_code=code) for code in payload.depends_on),
            actor=actor_code_of(request),
            correlation_id=uuid4(),
            now=now,
        )
    )
    return Status(201, task.to_view(today=now.date()))


@router.post(
    "/tasks/{task_code}/transition",
    response=TaskView,
    auth=actor_header,
    url_name="task_transition",
)
def post_task_transition(
    request: HttpRequest, task_code: str, payload: TaskTransitionIn
) -> TaskView:
    """Move a task along a declared edge of the task workflow.

    A task entering or leaving a ``BLOCKED``-category state changes its project's derived health,
    so the read model is rebuilt by the consumer reacting to ``task.state_changed`` and
    ``project.risk.changed`` may follow on the stream. None of that happens here: a slow rescore
    must not be able to roll back a legitimate move.
    """
    now = timezone.now()
    task = transition_task(
        TransitionTaskCommand(
            task_code=task_code,
            to_state_code=payload.to_state,
            reason=payload.reason,
            actor=actor_code_of(request),
            correlation_id=uuid4(),
            now=now,
        )
    )
    return task.to_view(today=now.date())


@router.post(
    "/projects/{project_code}/blockers",
    response={201: BlockerView},
    auth=actor_header,
    url_name="blocker_create",
)
def post_project_blocker(
    request: HttpRequest, project_code: str, payload: BlockerCreateIn
) -> Status[BlockerView]:
    """Raise an impediment against a project, or against one task of it.

    Raising a blocker does not change any workflow state. Blocking the work and recording why it is
    blocked are two facts, and collapsing them would make "unblock" ambiguous.
    """
    now = timezone.now()
    blocker = raise_blocker(
        RaiseBlockerCommand(
            project_code=project_code,
            task_code=payload.task_code,
            description=payload.description,
            kind=payload.kind,
            owner_code=payload.owner,
            actor=actor_code_of(request),
            correlation_id=uuid4(),
            now=now,
        )
    )
    return Status(201, blocker.to_view(now=now))


@router.post(
    "/blockers/{blocker_id}/resolve",
    response=BlockerView,
    auth=actor_header,
    url_name="blocker_resolve",
)
def post_blocker_resolve(
    request: HttpRequest, blocker_id: int, payload: BlockerResolveIn
) -> BlockerView:
    """Close an open blocker, stating how it was cleared.

    Resolving an already-resolved blocker is ``409 conflicting_state`` with
    ``details.current = "resolved"``, checked under a row lock — so it is a genuine second
    resolution rather than a lost update overwriting the first resolver's reason.

    A project whose last open blocker is cleared loses the ``BLOCKED`` risk flag on the next
    snapshot rebuild. The API does not change the workflow state as a side effect.
    """
    now = timezone.now()
    blocker = resolve_blocker(
        ResolveBlockerCommand(
            blocker_id=blocker_id,
            reason=payload.resolution,
            actor=actor_code_of(request),
            correlation_id=uuid4(),
            now=now,
        )
    )
    return blocker.to_view(now=now)


@router.post(
    "/projects/{project_code}/notes",
    response={201: NoteView},
    auth=actor_header,
    url_name="note_create",
)
def post_project_note(request: HttpRequest, project_code: str, payload: NoteIn) -> Status[NoteView]:
    """Append a chronological comment to a project, or to one task of it."""
    note = add_note(
        AddNoteCommand(
            project_code=project_code,
            task_code=payload.task_code,
            body=payload.body,
            actor=actor_code_of(request),
            correlation_id=uuid4(),
            now=timezone.now(),
        )
    )
    return Status(201, note.to_view())
