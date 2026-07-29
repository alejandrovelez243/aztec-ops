"""Use case: decide which lifecycle one task follows, or hand it back to its engagement type."""

from datetime import datetime
from typing import Final
from uuid import UUID

from django.db import transaction

from apps.activity.domain.value_objects import ActivityCommand
from apps.activity.models import ActivityRecord
from apps.activity.services import write_activity
from apps.events.domain.envelope import ENTITY_TASK, TOPIC_TASK_UPDATED
from apps.events.services import enqueue_event
from apps.work.domain.errors import TaskNotFound
from apps.work.models import Task
from apps.workflow.domain.errors import WorkflowNotFound
from apps.workflow.models import AppliesTo, Workflow
from apps.workflow.services.reassignment import validate_reassignment

#: ``ActivityRecord.entity_type`` for a fact about a task. Repeated from ``transition_task`` rather
#: than shared, exactly as that module repeats it: a constant imported across use cases is a
#: coupling between two transaction boundaries that have no other reason to know about each other.
ACTIVITY_ENTITY_TASK: Final = "task"


@transaction.atomic
def assign_task_workflow(
    *,
    task_code: str,
    workflow_code: str | None,
    actor: str,
    correlation_id: UUID,
    now: datetime,
) -> Task:
    """Put a task on a named lifecycle, or clear the assignment so it inherits one again.

    The task counterpart of ``assign_project_workflow`` and built the same way, because the decision
    it implements is the same one: it changes which graph governs the task and never which state the
    task is on. The state ``code`` is identical before and after — the task is repointed at the
    equivalent node of the target graph — so this route cannot be used to reach a state no edge
    leads to, and CLAUDE.md rule 2 is untouched: moving a task is still ``transition_task``,
    validated against ``WorkflowTransition``.

    **The failure mode.** A target graph with no active state carrying the task's current state code
    is refused with :class:`~apps.workflow.domain.errors.IncompatibleWorkflowState`, naming the task,
    its state, both graphs and the states the target does offer. Accepting a landing state from the
    caller instead was rejected for the reason ``apps.workflow.services.reassignment`` sets out: it
    would be an unvalidated ``workflow_state`` write over HTTP. Nothing lands silently — a task
    standing on a state its own workflow does not contain is a corrupted aggregate, so this either
    lands it on a state of the new graph or changes nothing.

    ``workflow_code=None`` clears the assignment, and the graph the binding ladder then hands the
    task is compatibility-checked exactly like a named one.

    A reassignment that changes nothing writes no ``ActivityRecord`` and no ``OutboxEvent``.

    Args:
        task_code: Business code, e.g. ``PRJ-01-T02``.
        workflow_code: ``Workflow.code`` to follow, or ``None`` to go back to inheriting one.
        actor: ``accounts.User.code`` of the ops lead making the decision.
        correlation_id: Threaded from the API boundary.
        now: Domain time, supplied by the caller.

    Returns:
        The task as it stands after the reassignment.

    Raises:
        TaskNotFound: No task carries ``task_code``, or it was removed (ADR 0012) — a task out of
            the operation's attention has no lifecycle worth deciding.
        WorkflowNotFound: ``workflow_code`` matches no graph.
        WorkflowKindMismatch: The named graph governs projects, not tasks.
        WorkflowRetired: The named graph is out of service and takes no arrivals.
        IncompatibleWorkflowState: The target has no active state matching the task's current one.
        WorkflowNotConfigured: ``workflow_code`` was ``None`` and no binding or default answers for
            tasks.
    """
    task = Task.objects.active().locked().with_relations().for_code(task_code).first()
    if task is None:
        raise TaskNotFound(task_code)

    requested = _workflow_named(workflow_code)
    if task.workflow_id == (requested.pk if requested is not None else None):
        return task

    target = Workflow.objects.resolve(
        applies_to=AppliesTo.TASK,
        engagement_type_id=task.project.engagement_type_id,
        assigned=requested,
    )
    check = validate_reassignment(entity=task, target=target, applies_to=AppliesTo.TASK)

    task.workflow = requested
    task.workflow_state_id = check.landing_state_id
    task.save(update_fields=["workflow", "workflow_state", "updated_at"])

    write_activity(
        ActivityCommand(
            entity_type=ACTIVITY_ENTITY_TASK,
            entity_id=task.code,
            verb=ActivityRecord.Verb.WORKFLOW_ASSIGNED,
            actor=actor,
            from_value=check.from_workflow_code,
            to_value=check.to_workflow_code,
            metadata={
                "project_code": task.project.code,
                "source": "DIRECT" if requested is not None else "INHERITED",
                "state": check.state_code,
            },
            occurred_at=now,
            correlation_id=correlation_id,
        )
    )
    # EVENTS.md §4 ``task.updated``: ``project_code`` so a consumer aggregates without a foreign key
    # into this context, plus the changes map keyed by model field name. ``workflow_state`` is
    # deliberately absent — the state code did not move.
    enqueue_event(
        topic=TOPIC_TASK_UPDATED,
        entity_type=ENTITY_TASK,
        entity_id=task.code,
        payload={
            "project_code": task.project.code,
            "changes": {
                "workflow": {
                    "from": check.from_workflow_code,
                    "to": check.to_workflow_code,
                }
            },
        },
        actor=actor,
        correlation_id=correlation_id,
        occurred_at=now,
    )

    # Re-read so the returned task carries the landing state's row rather than the stale related
    # object cached from before the assignment.
    reassigned = Task.objects.with_relations().with_dependencies().for_code(task_code).first()
    if reassigned is None:  # pragma: no cover - the row is locked inside this transaction
        raise TaskNotFound(task_code)
    return reassigned


def _workflow_named(workflow_code: str | None) -> Workflow | None:
    """Resolve the graph the caller named, or ``None`` when they named none.

    Args:
        workflow_code: ``Workflow.code``, or ``None`` to clear the assignment.

    Returns:
        The graph, or ``None``.

    Raises:
        WorkflowNotFound: A code was given and no graph carries it. A retired graph resolves here
            and is refused by name later, rather than being reported as missing.
    """
    if workflow_code is None:
        return None
    workflow = Workflow.objects.filter(code=workflow_code).first()
    if workflow is None:
        raise WorkflowNotFound(workflow_code)
    return workflow
