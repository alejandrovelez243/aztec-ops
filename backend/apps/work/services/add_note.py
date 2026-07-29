"""Use case: append a chronological comment to a project or one of its tasks."""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from django.db import transaction

from apps.accounts.models import User
from apps.activity.domain.value_objects import ActivityCommand
from apps.activity.services import write_activity
from apps.events.domain.envelope import ENTITY_NOTE, TOPIC_NOTE_ADDED
from apps.events.services import enqueue_event
from apps.portfolio.models import Project
from apps.work.domain.errors import ProjectNotFound, TaskNotFound, TaskOutsideProject
from apps.work.models import Note, Task
from apps.work.services import ORIGIN_SYSTEM

if TYPE_CHECKING:
    from apps.work.domain.commands import AddNoteCommand

#: ``ActivityRecord.entity_type`` for a fact about a project.
ACTIVITY_ENTITY_PROJECT: Final = "project"


@transaction.atomic
def add_note(command: AddNoteCommand) -> Note:
    """Write a note against a project, naming the task it refers to when there is one.

    ``project`` is copied from the task for task-scoped notes — the same rule as ``Blocker``
    and for the same reason: the project timeline must find every note with one indexed read
    and never a join through ``Task``. A task naming a different project is rejected rather
    than reparented.

    The event's ``entity`` is the note — ``{"type": "note", "id": "NOTE-0391"}``, the code
    minted by ``Note.save()`` — and not the project, which is what EVENTS.md §4 has always
    specified. ``payload.project_code`` is unchanged, so a consumer that resolves the event to a
    project is unaffected by the entity type moving from ``project`` to ``note``.

    The audit record is written against the project even for a task note, because
    ``NOTE_ADDED`` is what the project timeline renders; the task is named in ``metadata``.
    The payload carries exactly the fields EVENTS.md §4 lists — ``body`` included, so the
    timeline appends live without a second fetch — and never the note's primary key, which
    would be the foreign key into this context that §1 forbids.

    Args:
        command: Validated input. ``body`` is already known to be non-empty.

    Returns:
        The persisted note.

    Raises:
        ProjectNotFound: ``project_code`` does not resolve.
        TaskNotFound: ``task_code`` was given and does not resolve.
        TaskOutsideProject: The named task belongs to a different project.
    """
    project = Project.objects.filter(code=command.project_code).first()
    if project is None:
        raise ProjectNotFound(command.project_code)

    task = _resolve_task(command.task_code, project_id=project.pk, project_code=project.code)
    note = Note.objects.create(project=project, task=task, body=command.body, author=command.actor)

    write_activity(
        ActivityCommand(
            entity_type=ACTIVITY_ENTITY_PROJECT,
            entity_id=project.code,
            verb="NOTE_ADDED",
            origin=ORIGIN_SYSTEM,
            actor=command.actor,
            metadata={"note_id": note.pk, "task_code": task.code if task else ""},
            occurred_at=command.now,
            correlation_id=command.correlation_id,
        )
    )
    enqueue_event(
        topic=TOPIC_NOTE_ADDED,
        entity_type=ENTITY_NOTE,
        entity_id=note.code,
        payload={
            "project_code": project.code,
            "task_code": task.code if task else None,
            "body": note.body,
            "author_alias": _author_alias(note.author),
        },
        actor=command.actor,
        correlation_id=command.correlation_id,
        occurred_at=command.now,
    )
    return note


def _author_alias(author_code: str) -> str:
    """Render the note's author for the `note.added` payload, which names people by alias.

    ``Note.author`` stays a denormalized code string rather than becoming a foreign key now that
    people are ``accounts.User`` rows, so the alias has to be looked up at publish time. That is
    the point: a note outlives its author's row, and ``system`` is a legitimate author that no
    row will ever carry.

    Falls back to the code when the actor resolves to nobody — ``system`` and an imported author
    are both legitimate — because a missing person must not stop a note from being published.

    Args:
        author_code: ``Note.author``, an ``accounts.User.code`` or a non-human origin.

    Returns:
        The person's display alias, or ``author_code`` unchanged when it names nobody.
    """
    member = User.objects.with_role().by_code(author_code).first()
    return member.alias if member is not None else author_code


def _resolve_task(task_code: str | None, *, project_id: int, project_code: str) -> Task | None:
    if task_code is None:
        return None
    task = Task.objects.for_code(task_code).first()
    if task is None:
        raise TaskNotFound(task_code)
    if task.project_id != project_id:
        raise TaskOutsideProject(task_code, project_code)
    return task
