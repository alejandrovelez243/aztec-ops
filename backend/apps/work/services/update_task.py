"""Use case: edit the mutable fields of a task."""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from django.db import transaction

from apps.accounts.repositories import user_by_code
from apps.activity.domain.value_objects import ActivityCommand
from apps.activity.services import write_activity
from apps.events.domain.changes import render_change_value
from apps.events.domain.envelope import ENTITY_TASK, TOPIC_TASK_UPDATED
from apps.events.services import enqueue_event
from apps.work.domain.errors import PersonNotFound, PriorityNotFound, TaskNotFound
from apps.work.domain.value_objects import FieldChange
from apps.work.repositories import priority_by_code, task_repository
from apps.work.services import ORIGIN_SYSTEM

if TYPE_CHECKING:
    from apps.work.domain.commands import UpdateTaskCommand
    from apps.work.models import Task

#: ``ActivityRecord.entity_type`` for a fact about a task.
ACTIVITY_ENTITY_TASK: Final = "task"

#: Fields whose movement is worth an audit record, and the verb that records it. A registry
#: rather than a chain of ``if``s: auditing another field is one entry here. ``verb`` is a
#: closed structural vocabulary, which is why editing ``due_date``, ``title`` or ``detail``
#: deliberately produces no record — inventing a verb for them would be a migration.
_AUDITED_FIELDS: Final[dict[str, str]] = {
    "priority": "PRIORITY_CHANGED",
    "assignee": "OWNER_CHANGED",
}

#: Free-text columns copied straight through when present in the payload.
_TEXT_FIELDS: Final[tuple[str, ...]] = ("title", "detail", "last_progress")


@transaction.atomic
def update_task(command: UpdateTaskCommand) -> Task:
    """Apply the fields present in the payload, and nothing else.

    A field absent from the command is left untouched; a field present and ``None`` is
    cleared — that is how a task is unassigned, and it is why the command reads Pydantic's
    ``model_fields_set`` instead of treating ``None`` as "no change". ``workflow_state`` is
    not updatable here at any price: a state change goes through ``transition_task``, so
    this service cannot move a task along an edge that does not exist.

    A call that changes nothing is a no-op — no audit record, no ``updated_at`` bump, no
    ``OutboxEvent``. Recording an unchanged save would put noise in the one table nobody is
    allowed to clean, and an event for it would teach every consumer to recompute for nothing.

    A real change emits ``task.updated`` in this same transaction, naming every field that
    moved with its before and after value. That is what carries a priority or due-date edit to
    the priority recalculator: neither is a workflow transition, and ``clock.ticked`` does not
    rescue the score either, since ``PriorityScore.valid_until`` only moves when something
    recomputes it.

    Args:
        command: Validated input, carrying the actor, the correlation id and domain time.

    Returns:
        The task as persisted, whether or not anything moved.

    Raises:
        TaskNotFound: ``task_code`` does not resolve.
        PriorityNotFound: ``priority_code`` was given and is not in the taxonomy.
        PersonNotFound: ``assignee_code`` was given and is not on the roster.
    """
    task = task_repository.get_for_update(command.task_code)
    if task is None:
        raise TaskNotFound(command.task_code)

    changes = (
        *_apply_scalar_changes(task, command),
        *_apply_priority_change(task, command),
        *_apply_assignee_change(task, command),
    )
    if not changes:
        return task

    task.save(update_fields=[*(change.field for change in changes), "updated_at"])

    for change in changes:
        verb = _AUDITED_FIELDS.get(change.field)
        if verb is None:
            continue
        write_activity(
            ActivityCommand(
                entity_type=ACTIVITY_ENTITY_TASK,
                entity_id=task.code,
                verb=verb,
                origin=ORIGIN_SYSTEM,
                actor=command.actor,
                from_value=change.from_value,
                to_value=change.to_value,
                metadata={"project_code": task.project.code, "field": change.field},
                occurred_at=command.now,
                correlation_id=command.correlation_id,
            )
        )

    # EVENTS.md §4 `task.updated`: `project_code` so a consumer aggregates without a foreign key
    # into this context, and one non-empty `changes` object keyed by model field name.
    enqueue_event(
        topic=TOPIC_TASK_UPDATED,
        entity_type=ENTITY_TASK,
        entity_id=task.code,
        payload={
            "project_code": task.project.code,
            "changes": {change.field: change.as_payload() for change in changes},
        },
        actor=command.actor,
        correlation_id=command.correlation_id,
        occurred_at=command.now,
    )

    return task


def _apply_scalar_changes(task: Task, command: UpdateTaskCommand) -> tuple[FieldChange, ...]:
    """Copy the text and date columns the payload actually carries onto the task.

    Each pair is rendered before the attribute is overwritten, so the "from" side is the value
    that was actually persisted rather than the one the caller just supplied.
    """
    changes: list[FieldChange] = []

    for field in _TEXT_FIELDS:
        if field not in command.model_fields_set:
            continue
        new_value = getattr(command, field)
        old_value = getattr(task, field)
        if new_value == old_value:
            continue
        setattr(task, field, new_value)
        changes.append(
            FieldChange(
                field=field,
                before=render_change_value(old_value),
                after=render_change_value(new_value),
            )
        )

    if "due_date" in command.model_fields_set and command.due_date != task.due_date:
        changes.append(
            FieldChange(
                field="due_date",
                before=render_change_value(task.due_date),
                after=render_change_value(command.due_date),
            )
        )
        task.due_date = command.due_date

    return tuple(changes)


def _apply_priority_change(task: Task, command: UpdateTaskCommand) -> tuple[FieldChange, ...]:
    """Repoint the task at another ``catalog.Priority``, refusing an unknown code."""
    if "priority_code" not in command.model_fields_set:
        return ()
    if command.priority_code == task.priority.code:
        return ()

    priority = priority_by_code(command.priority_code)
    if priority is None:
        raise PriorityNotFound(command.priority_code)

    change = FieldChange(
        field="priority",
        before=render_change_value(task.priority),
        after=render_change_value(priority),
    )
    task.priority = priority
    return (change,)


def _apply_assignee_change(task: Task, command: UpdateTaskCommand) -> tuple[FieldChange, ...]:
    """Assign, reassign or unassign the task; an explicit ``None`` means unassign."""
    if "assignee_code" not in command.model_fields_set:
        return ()

    current_code = task.assignee.code if task.assignee else None
    if command.assignee_code == current_code:
        return ()

    if command.assignee_code is None:
        change = FieldChange(field="assignee", before=current_code, after=None)
        task.assignee = None
        return (change,)

    assignee = user_by_code(command.assignee_code)
    if assignee is None:
        raise PersonNotFound(command.assignee_code)

    change = FieldChange(field="assignee", before=current_code, after=render_change_value(assignee))
    task.assignee = assignee
    return (change,)
