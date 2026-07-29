"""Use case: add a task to a project."""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from django.db import transaction

from apps.accounts.models import User
from apps.activity.domain.value_objects import ActivityCommand
from apps.activity.services import write_activity
from apps.catalog.models import Priority
from apps.events.domain.envelope import ENTITY_TASK, TOPIC_TASK_CREATED
from apps.events.services import enqueue_event
from apps.portfolio.models import Project
from apps.work.domain.dependencies import find_dependency_cycle
from apps.work.domain.errors import (
    DependencyCycle,
    DependencyOutsideProject,
    PersonNotFound,
    PriorityNotFound,
    ProjectNotFound,
    TaskNotFound,
)
from apps.work.models import Task, TaskDependency
from apps.work.services import ORIGIN_SYSTEM
from apps.workflow.models import AppliesTo, Workflow, WorkflowState

if TYPE_CHECKING:
    from collections.abc import Sequence

    from apps.work.domain.commands import CreateTaskCommand, DependencySpec

#: ``ActivityRecord.entity_type`` for a fact about a project. The audit trail's set is
#: narrower than the event envelope's, so the two are named from their own modules.
ACTIVITY_ENTITY_PROJECT: Final = "project"


@transaction.atomic
def create_task(command: CreateTaskCommand) -> Task:
    """Create a task in its project's initial workflow state, with its prerequisites.

    The starting state is resolved from the project's ``WorkflowBinding`` rather than
    supplied by the caller, so a task cannot be entered directly into a state that no
    declared transition leads to. Dependencies are validated against the project's existing
    edges before any edge row is written, which is why a rejected cycle rolls the whole call
    back rather than leaving a task with half its prerequisites.

    The audit record is written against the *project*, with verb ``TASK_ADDED``: the
    timeline that renders on ``/projects/{code}`` is the one that has to show a task
    appearing. The event is emitted against the task and carries ``project_code``, so the
    snapshot rebuild resolves it to a project without reading this context's tables.

    ``command.code`` left empty means "allocate one": the code is minted as
    ``{project_code}-T{NN}`` inside this transaction, so a rollback frees the number and the
    caller never has to guess one. The HTTP API always leaves it empty; a fixture pins its own.

    Args:
        command: Validated input. ``now`` is domain time and ``correlation_id`` chains this
            creation to whatever movement caused it.

    Returns:
        The persisted task, with ``project``, ``priority`` and ``workflow_state`` populated.

    Raises:
        ProjectNotFound: ``project_code`` does not resolve.
        PriorityNotFound: ``priority_code`` is not in the ``catalog.Priority`` taxonomy.
        PersonNotFound: ``assignee_code`` was given and is not on the roster.
        TaskNotFound: A dependency named a task code that does not exist.
        DependencyOutsideProject: A dependency pointed at another project's task.
        DependencyCycle: A dependency would close a loop in the graph.
        WorkflowNotConfigured: No binding and no default workflow answers for tasks.
        InitialStateMissing: The resolved workflow has no state flagged ``is_initial``.
    """
    project = Project.objects.filter(code=command.project_code).first()
    if project is None:
        raise ProjectNotFound(command.project_code)

    priority = Priority.objects.filter(code=command.priority_code).first()
    if priority is None:
        raise PriorityNotFound(command.priority_code)

    assignee = _resolve_assignee(command.assignee_code)
    workflow = Workflow.objects.resolve(
        applies_to=AppliesTo.TASK, engagement_type_id=project.engagement_type_id
    )
    initial_state = WorkflowState.objects.entry_state(workflow_id=workflow.pk)

    # An empty code means "allocate one": the HTTP API never accepts a task code from a client,
    # while a fixture pins its own. Minted inside this transaction, so a rollback frees the number.
    task = Task.objects.create(
        code=command.code or Task.objects.next_code_for(project.code),
        project=project,
        assignee=assignee,
        priority=priority,
        workflow_state=initial_state,
        due_date=command.due_date,
        title=command.title,
        detail=command.detail,
        last_progress=command.last_progress,
    )
    _link_dependencies(task=task, dependencies=command.dependencies)

    write_activity(
        ActivityCommand(
            entity_type=ACTIVITY_ENTITY_PROJECT,
            entity_id=project.code,
            verb="TASK_ADDED",
            origin=ORIGIN_SYSTEM,
            actor=command.actor,
            to_value=task.code,
            metadata={
                "task_code": task.code,
                "title": task.title,
                "priority": priority.code,
                "assignee": assignee.code if assignee else "",
            },
            occurred_at=command.now,
            correlation_id=command.correlation_id,
        )
    )
    enqueue_event(
        topic=TOPIC_TASK_CREATED,
        entity_type=ENTITY_TASK,
        entity_id=task.code,
        payload={
            "project_code": project.code,
            "title": task.title,
            "priority": priority.code,
            "state": initial_state.code,
            "assignee_alias": assignee.alias if assignee else None,
            "due_date": task.due_date.isoformat() if task.due_date else None,
            "depends_on": [
                {"task_code": spec.depends_on_code, "raw_label": spec.raw_label}
                for spec in command.dependencies
            ],
        },
        actor=command.actor,
        correlation_id=command.correlation_id,
        occurred_at=command.now,
    )
    return task


def _resolve_assignee(assignee_code: str | None) -> User | None:
    if assignee_code is None:
        return None
    assignee = User.objects.with_role().by_code(assignee_code).first()
    if assignee is None:
        raise PersonNotFound(assignee_code)
    return assignee


def _link_dependencies(*, task: Task, dependencies: Sequence[DependencySpec]) -> None:
    """Persist the task's prerequisites, refusing any that would close a loop.

    The adjacency mapping is extended as each spec is accepted, so two dependencies given
    in the same call cannot form a cycle between themselves — checking each one against the
    database state alone would let that pair through.
    """
    if not dependencies:
        return

    adjacency = TaskDependency.objects.for_project(task.project_id).resolved().adjacency()
    rows: list[TaskDependency] = []

    for spec in dependencies:
        if spec.depends_on_code is None:
            rows.append(TaskDependency(task=task, raw_label=spec.raw_label, is_resolved=False))
            continue

        target = Task.objects.for_code(spec.depends_on_code).first()
        if target is None:
            raise TaskNotFound(spec.depends_on_code)
        if target.project_id != task.project_id:
            raise DependencyOutsideProject(task.code, target.code)

        cycle = find_dependency_cycle(
            graph=adjacency, task_code=task.code, depends_on_code=target.code
        )
        if cycle is not None:
            raise DependencyCycle(cycle)

        adjacency = {**adjacency, task.code: (*adjacency.get(task.code, ()), target.code)}
        rows.append(
            TaskDependency(task=task, depends_on=target, raw_label=spec.raw_label, is_resolved=True)
        )

    TaskDependency.objects.bulk_create(rows)
