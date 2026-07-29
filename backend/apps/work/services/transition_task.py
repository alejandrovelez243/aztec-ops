"""Use case: move a task to another workflow state."""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from django.db import transaction

from apps.activity.domain.value_objects import ActivityCommand
from apps.activity.services import write_activity
from apps.events.domain.envelope import ENTITY_TASK, TOPIC_TASK_STATE_CHANGED
from apps.events.services import enqueue_event
from apps.work.domain.errors import TaskNotFound
from apps.work.models import Task
from apps.work.services import ORIGIN_MANUAL, ORIGIN_SYSTEM
from apps.workflow.services.transition import validate_transition

if TYPE_CHECKING:
    from apps.work.domain.commands import TransitionTaskCommand

#: ``ActivityRecord.entity_type`` for a fact about a task.
ACTIVITY_ENTITY_TASK: Final = "task"


@transaction.atomic
def transition_task(command: TransitionTaskCommand) -> Task:
    """Move a task along a declared edge of its workflow, then record and publish it.

    This service decides nothing about the state machine. It asks the workflow context to
    validate the move and only then assigns the state the check returned — that split is the
    workflow app's stated contract, and it is why an undeclared edge, an inactive one, a
    missing reason, an empty ``requires_fields`` value or a refusing guard all abort here
    with nothing written. Assigning ``workflow_state`` without that check is the single bug
    the whole workflow design exists to prevent.

    Recomputing the score and re-evaluating risk are deliberately *not* done here. They are
    consumers of ``task.state_changed``, so a slow or failing recalculation cannot roll back
    a legitimate state change.

    ``origin`` follows the reason: an operator who had to justify the move forced it, so the
    record is ``MANUAL``; a move the declared edge asked no questions about is ``SYSTEM``.

    Args:
        command: Validated input naming the task, the target state code and the reason.

    Returns:
        The task carrying its new state.

    Raises:
        TaskNotFound: ``task_code`` does not resolve, or names a task that was removed. A removed
            task has no legal moves at all (ADR 0012): it is out of the operation's attention, and
            answering with a state change would put work back on a board nobody is looking at.
        TransitionNotAllowed: No active edge leads from the current state to the target.
        ReasonRequired: The edge sets ``requires_reason`` and no reason was given.
        RequiredFieldMissing: A field named in ``requires_fields`` is empty on the task.
        GuardRejected: The edge's registered guard refused the move.
    """
    task = Task.objects.active().locked().with_relations().for_code(command.task_code).first()
    if task is None:
        raise TaskNotFound(command.task_code)

    check = validate_transition(
        entity=task,
        to_state_code=command.to_state_code,
        actor=command.actor,
        now=command.now,
        reason=command.reason,
    )

    task.workflow_state_id = check.to_state_id
    task.save(update_fields=["workflow_state", "updated_at"])

    origin = ORIGIN_MANUAL if command.reason.strip() else ORIGIN_SYSTEM
    write_activity(
        ActivityCommand(
            entity_type=ACTIVITY_ENTITY_TASK,
            entity_id=task.code,
            verb="STATE_CHANGED",
            origin=origin,
            actor=command.actor,
            from_value=check.from_state_code,
            to_value=check.to_state_code,
            reason=command.reason,
            metadata={
                "project_code": task.project.code,
                "transition_id": check.transition_id,
                "to_state_category": check.to_state_category,
            },
            occurred_at=command.now,
            correlation_id=command.correlation_id,
        )
    )
    enqueue_event(
        topic=TOPIC_TASK_STATE_CHANGED,
        entity_type=ENTITY_TASK,
        entity_id=task.code,
        payload={
            "project_code": task.project.code,
            "from": check.from_state_code,
            "to": check.to_state_code,
            "from_category": check.from_state_category,
            "to_category": check.to_state_category,
            "transition": check.edge_code,
            "reason": command.reason or None,
            "last_progress": task.last_progress or None,
        },
        actor=command.actor,
        correlation_id=command.correlation_id,
        occurred_at=command.now,
    )
    return task
