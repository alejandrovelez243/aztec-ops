"""Use case: edit the mutable fields of a task, including removing and restoring it.

Removal is a soft delete (ADR 0012) and it lives here rather than in a service of its own, for
the reason ``accounts.update_member`` gives about retiring a person: ``DELETE`` and the ``PATCH``
that puts the task back are the same field moving in two directions, and two services would
eventually disagree about what else a removal touches.
"""

from __future__ import annotations

from collections import Counter
from typing import TYPE_CHECKING, Final

from django.db import transaction

from apps.accounts.models import User
from apps.activity.domain.value_objects import ActivityCommand
from apps.activity.services import write_activity
from apps.catalog.models import Priority
from apps.events.domain.changes import render_change_value
from apps.events.domain.envelope import (
    ENTITY_TASK,
    TOPIC_TASK_ARCHIVE_CHANGED,
    TOPIC_TASK_UPDATED,
)
from apps.events.services import enqueue_event
from apps.work.domain.dependencies import find_dependency_cycle
from apps.work.domain.errors import (
    DependencyCycle,
    DependencyOutsideProject,
    PersonNotFound,
    PriorityNotFound,
    TaskNotFound,
)
from apps.work.domain.events import TaskArchiveChangedPayload
from apps.work.domain.value_objects import FieldChange
from apps.work.models import Task, TaskDependency
from apps.work.services import ORIGIN_SYSTEM

if TYPE_CHECKING:
    from collections.abc import Sequence

    from pydantic import JsonValue

    from apps.work.domain.commands import DependencySpec, UpdateTaskCommand

#: ``ActivityRecord.entity_type`` for a fact about a task.
ACTIVITY_ENTITY_TASK: Final = "task"

#: ``ActivityRecord.entity_type`` for a fact about a project. A removal is recorded against the
#: project, not the task — see :func:`_record_archive_change`.
ACTIVITY_ENTITY_PROJECT: Final = "project"

#: Fields whose movement is worth an audit record, and the verb that records it. A registry
#: rather than a chain of ``if``s: auditing another field is one entry here. ``verb`` is a
#: closed structural vocabulary, which is why editing ``due_date``, ``title`` or ``detail``
#: deliberately produces no record — inventing a verb for them would be a migration.
_AUDITED_FIELDS: Final[dict[str, str]] = {
    "priority": "PRIORITY_CHANGED",
    "assignee": "OWNER_CHANGED",
}

#: The name the prerequisite set travels under, in ``changes`` and in the audit metadata. It is
#: the wire field rather than a model column: ``depends_on`` is a set of ``TaskDependency`` rows,
#: so it can never appear in ``Task.save(update_fields=...)``.
_DEPENDENCY_FIELD: Final = "depends_on"

#: Width of ``ActivityRecord.from_value``/``to_value``. A prerequisite set is rendered into those
#: columns as one line and truncated to fit; the full before and after lists travel in ``metadata``,
#: which is JSONB and loses nothing.
_AUDIT_VALUE_MAX_LENGTH: Final = 255

#: Separator between prerequisites in that one-line rendering.
_DEPENDENCY_SEPARATOR: Final = ", "

#: The command attribute the prerequisite set arrives on. Not the same string as
#: :data:`_DEPENDENCY_FIELD`: the command names what it carries — classified specs — and the wire
#: and the audit name the relation those specs become.
_DEPENDENCY_COMMAND_FIELD: Final = "dependencies"

#: Free-text columns copied straight through when present in the payload.
_TEXT_FIELDS: Final[tuple[str, ...]] = (
    "title",
    "detail",
    "description",
    "last_progress",
)


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
    That is also what makes ``DELETE /tasks/{code}`` idempotent: removing an already-removed
    task moves nothing, so the second call writes no second record and emits no second event.

    ``is_archived`` travels on its own topic, ``task.archive_changed``, and never inside
    ``task.updated``'s ``changes`` — a subscriber asking "does this task still count?" gets a
    field it can branch on rather than a key it has to look for. A single ``PATCH`` moving both
    a removal and an attribute therefore emits both events: two facts happened.

    ``dependencies`` replaces the task's whole prerequisite set when it is present, including
    with the empty tuple, which clears it. The acyclicity check runs here exactly as it does on
    creation and against the graph **as it will stand after the replacement**: an edit is the
    easier way to close a loop — the task already has prerequisites and already is one — so a
    check only on the creation path would leave the invariant enforced where it is hardest to
    violate and absent where it is easiest.

    **The task is read without the removed rows being filtered out**, which is deliberate and is
    the one write in the product for which that is true. Restoring a task means finding one that
    is already removed, and re-removing one has to answer "nothing changed" rather than "no such
    task" — a ``.active()`` here would turn idempotency into a 404.

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
        TaskNotFound: ``task_code`` does not resolve, or a prerequisite named a task code that
            does not exist or was removed from the operation.
        PriorityNotFound: ``priority_code`` was given and is not in the taxonomy.
        PersonNotFound: ``assignee_code`` was given and is not on the roster.
        DependencyOutsideProject: A prerequisite pointed at another project's task.
        DependencyCycle: The replacement set would close a loop in the project's graph.
    """
    task = Task.objects.locked().with_relations().for_code(command.task_code).first()
    if task is None:
        raise TaskNotFound(command.task_code)

    changes = (
        *_apply_scalar_changes(task, command),
        *_apply_priority_change(task, command),
        *_apply_assignee_change(task, command),
    )
    archive_changed = _apply_archive_change(task, command)
    dependency_change = _replace_dependencies(task=task, command=command)
    if not changes and not archive_changed and dependency_change is None:
        return task

    moved = [change.field for change in changes]
    if archive_changed:
        moved.append("is_archived")
    # ``depends_on`` is deliberately absent from ``moved``: it is a set of rows, not a column, and
    # the save happens anyway so a dependency edit still bumps ``updated_at``.
    task.save(update_fields=[*moved, "updated_at"])

    if archive_changed:
        _record_archive_change(task, command)
    if dependency_change is not None:
        _record_dependency_change(task, command, dependency_change)

    announced = changes if dependency_change is None else (*changes, dependency_change)
    if not announced:
        return task

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
            "changes": {change.field: change.as_payload() for change in announced},
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


def _apply_archive_change(task: Task, command: UpdateTaskCommand) -> bool:
    """Mutate ``is_archived`` in memory and report whether it moved.

    Kept out of :func:`_apply_scalar_changes` on purpose, exactly as ``accounts`` keeps activation
    out of its attribute pass: removal is a different fact with a different verb and a different
    topic, and folding it into the ``changes`` map would put it inside ``task.updated`` where no
    subscriber is looking for it.

    Args:
        task: The locked row, mutated in place and not saved here.
        command: The requested edit.

    Returns:
        Whether the task's presence in the operation's attention changed. ``False`` for a request
        that removes an already-removed task, which is what makes ``DELETE`` idempotent.
    """
    if "is_archived" not in command.model_fields_set or command.is_archived is None:
        return False
    if command.is_archived == task.is_archived:
        return False
    task.is_archived = command.is_archived
    return True


def _record_archive_change(task: Task, command: UpdateTaskCommand) -> None:
    """Write the removal-or-restoration fact and announce it.

    The record hangs off the **project**, with ``entity_type="project"``, mirroring the
    ``TASK_ADDED`` that ``create_task`` writes: the timeline an operator reads on
    ``/projects/{code}`` is the one that has to show a task appearing and disappearing, and a
    record filed under a task nobody can open any more is a record nobody reads.

    The task code is in ``from_value`` here where ``TASK_ADDED`` puts it in ``to_value``, and the
    asymmetry is the point rather than an oversight: the trail reads as "this task was added *to*
    the project" and "this task was removed *from* it", so a restoration is a ``to_value`` again.
    ``metadata.is_archived`` carries the same fact as a boolean for anything reading the row
    without a table of verbs.
    """
    removed = task.is_archived
    write_activity(
        ActivityCommand(
            entity_type=ACTIVITY_ENTITY_PROJECT,
            entity_id=task.project.code,
            verb="TASK_REMOVED" if removed else "TASK_RESTORED",
            origin=ORIGIN_SYSTEM,
            actor=command.actor,
            from_value=task.code if removed else "",
            to_value="" if removed else task.code,
            metadata={
                "task_code": task.code,
                "title": task.title,
                "is_archived": removed,
            },
            occurred_at=command.now,
            correlation_id=command.correlation_id,
        )
    )
    enqueue_event(
        topic=TOPIC_TASK_ARCHIVE_CHANGED,
        entity_type=ENTITY_TASK,
        entity_id=task.code,
        payload=TaskArchiveChangedPayload(
            project_code=task.project.code,
            title=task.title,
            is_archived=task.is_archived,
        ).model_dump(mode="json"),
        actor=command.actor,
        correlation_id=command.correlation_id,
        occurred_at=command.now,
    )


def _replace_dependencies(*, task: Task, command: UpdateTaskCommand) -> FieldChange | None:
    """Make the task's prerequisites exactly the ones the payload names, and report the move.

    Absent means untouched; any tuple replaces the whole set, the empty one included. The
    replacement is a diff and not a delete-then-recreate: an edge the caller re-sent keeps its
    row and its ``created_at``, so "waiting on this since the 3rd" survives an edit that only
    added a second prerequisite. Rows are matched as multisets of
    ``(target code, raw label)``, because two prose prerequisites with the same text are two
    legitimate rows — the partial unique constraint deliberately does not cover them.

    Args:
        task: The locked row whose edges are being redrawn.
        command: The requested edit.

    Returns:
        The before/after pair as a :class:`FieldChange` on ``depends_on``, or ``None`` when the
        payload left the set alone or asked for the set it already has. ``before`` and ``after``
        are lists of strings, one per prerequisite: a set-valued field's honest rendering is the
        set, not a scalar.

    Raises:
        TaskNotFound: A prerequisite named a task code that resolves to nothing, or to a task
            removed from the operation.
        DependencyOutsideProject: A prerequisite pointed at another project's task.
        DependencyCycle: The replacement would close a loop in the project's graph.
    """
    if _DEPENDENCY_COMMAND_FIELD not in command.model_fields_set or command.dependencies is None:
        return None

    existing = list(task.dependencies.with_target())
    existing_keys = [
        (row.depends_on.code if row.depends_on is not None else None, row.raw_label)
        for row in existing
    ]
    desired = _resolve_dependencies(task=task, specs=command.dependencies)
    desired_keys = [
        (target.code if target is not None else None, spec.raw_label) for target, spec in desired
    ]
    if Counter(existing_keys) == Counter(desired_keys):
        return None

    surplus = Counter(existing_keys) - Counter(desired_keys)
    for row, key in zip(existing, existing_keys, strict=True):
        if surplus[key] > 0:
            surplus[key] -= 1
            row.delete()

    shortfall = Counter(desired_keys) - Counter(existing_keys)
    added: list[TaskDependency] = []
    for (target, spec), key in zip(desired, desired_keys, strict=True):
        if shortfall[key] <= 0:
            continue
        shortfall[key] -= 1
        added.append(
            TaskDependency(
                task=task,
                depends_on=target,
                raw_label=spec.raw_label,
                is_resolved=target is not None,
            )
        )
    TaskDependency.objects.bulk_create(added)

    return FieldChange(
        field=_DEPENDENCY_FIELD,
        before=[_render_edge(*key) for key in existing_keys],
        after=[_render_edge(*key) for key in desired_keys],
    )


def _resolve_dependencies(
    *, task: Task, specs: Sequence[DependencySpec]
) -> list[tuple[Task | None, DependencySpec]]:
    """Pair each requested prerequisite with the task it names, refusing an illegal graph.

    The adjacency the cycle check reads is the project's edges **minus this task's own**, because
    those are precisely the ones being replaced: the question is whether the graph *after* the
    edit is acyclic, and an edge that is about to be deleted is not part of it. It is then
    extended as each new edge is accepted, so two prerequisites given in the same call cannot
    form a loop between themselves either — checking each against the stored graph alone would
    let that pair through, which is the reason ``create_task`` extends it the same way.

    The existing edges are read unscoped by ``is_archived`` (see ``TaskDependencyQuerySet``)
    while a *new* target must be an unremoved task: nothing will ever move removed work, so a
    prerequisite pointing at it would never clear (ADR 0012).
    """
    adjacency = {
        code: targets
        for code, targets in TaskDependency.objects.for_project(task.project_id)
        .resolved()
        .adjacency()
        .items()
        if code != task.code
    }
    resolved: list[tuple[Task | None, DependencySpec]] = []

    for spec in specs:
        if spec.depends_on_code is None:
            resolved.append((None, spec))
            continue

        target = Task.objects.active().for_code(spec.depends_on_code).first()
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
        resolved.append((target, spec))

    return resolved


def _render_edge(depends_on_code: str | None, raw_label: str) -> str:
    """Name one prerequisite the way a reader of the trail identifies it.

    The task code when the edge resolved, the operation's own words when it did not — never
    both. A resolved edge keeps its ``raw_label`` in the database because that is the sentence
    somebody wrote, but the code is what identifies the prerequisite once it has one.
    """
    return depends_on_code if depends_on_code is not None else raw_label


def _record_dependency_change(task: Task, command: UpdateTaskCommand, change: FieldChange) -> None:
    """Write the fact that a task's prerequisites were redrawn.

    Its own verb, ``DEPENDENCIES_CHANGED``, rather than a row in the ``_AUDITED_FIELDS`` shape
    that ``priority`` and ``assignee`` use. Those two are scalar columns whose ``from_value`` and
    ``to_value`` are the whole fact; a prerequisite set is a collection, and squeezing it into
    that shape would mean either a generic verb — which ``apps.activity.models`` refuses on
    purpose, "each verb names *which* fact moved" — or a verb whose two text columns silently
    truncate the answer. Here the one-line rendering is a convenience for the timeline and the
    complete before and after lists travel in ``metadata``, which is JSONB and loses nothing.

    The record hangs off the **task**, unlike removal: a dependency is a statement about this
    piece of work and about one other, and the project timeline would carry it for every edge of
    every task without anyone having asked.
    """
    write_activity(
        ActivityCommand(
            entity_type=ACTIVITY_ENTITY_TASK,
            entity_id=task.code,
            verb="DEPENDENCIES_CHANGED",
            origin=ORIGIN_SYSTEM,
            actor=command.actor,
            from_value=_render_edges(change.before),
            to_value=_render_edges(change.after),
            metadata={
                "project_code": task.project.code,
                "field": _DEPENDENCY_FIELD,
                "before": change.before,
                "after": change.after,
            },
            occurred_at=command.now,
            correlation_id=command.correlation_id,
        )
    )


def _render_edges(labels: JsonValue) -> str:
    """Render a prerequisite list as the one line ``ActivityRecord`` can hold.

    Truncated to the column width rather than left to fail: the audit row is written inside the
    transaction that performs the edit, so an over-long rendering that reached the database would
    roll back a legitimate edit over a display detail. Nothing is lost — ``metadata`` carries the
    untruncated lists.

    Args:
        labels: The rendered prerequisite names, as they travel on :class:`FieldChange`, whose
            ``before``/``after`` are typed as JSON values. A non-list is rendered as empty.
    """
    if not isinstance(labels, list):
        return ""
    line = _DEPENDENCY_SEPARATOR.join(str(label) for label in labels)
    return line[:_AUDIT_VALUE_MAX_LENGTH]


def _apply_priority_change(task: Task, command: UpdateTaskCommand) -> tuple[FieldChange, ...]:
    """Repoint the task at another ``catalog.Priority``, refusing an unknown code."""
    if "priority_code" not in command.model_fields_set:
        return ()
    if command.priority_code == task.priority.code:
        return ()

    priority = Priority.objects.filter(code=command.priority_code).first()
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

    assignee = User.objects.with_role().by_code(command.assignee_code).first()
    if assignee is None:
        raise PersonNotFound(command.assignee_code)

    change = FieldChange(field="assignee", before=current_code, after=render_change_value(assignee))
    task.assignee = assignee
    return (change,)
