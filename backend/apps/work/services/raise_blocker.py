"""Use case: record an impediment against a project or one of its tasks."""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from django.db import transaction

from apps.accounts.models import User
from apps.activity.domain.value_objects import ActivityCommand
from apps.activity.services import write_activity
from apps.events.domain.envelope import ENTITY_BLOCKER, TOPIC_BLOCKER_RAISED
from apps.events.services import enqueue_event
from apps.portfolio.models import Project
from apps.work.domain.errors import (
    PersonNotFound,
    ProjectNotFound,
    TaskNotFound,
    TaskOutsideProject,
)
from apps.work.models import Blocker, Task
from apps.work.services import ORIGIN_MANUAL

if TYPE_CHECKING:
    from apps.work.domain.commands import RaiseBlockerCommand

#: ``ActivityRecord.entity_type`` for a fact about a blocker. ``entity_id`` stays the
#: *project* code, so the project timeline shows the impediment without a second query.
ACTIVITY_ENTITY_BLOCKER: Final = "blocker"


@transaction.atomic
def raise_blocker(command: RaiseBlockerCommand) -> Blocker:
    """Open a blocker, always attached to a project and optionally to one of its tasks.

    A task-level blocker still carries the project, cross-checked against the task rather
    than taken on trust: that is the rule ``DATA_MODEL`` §9.3 places in this layer, because
    a cross-row check would otherwise need a trigger. The column being always populated is
    what lets the risk evaluator, the blockage signal and the open-blockers panel find a
    blocker's project without joining through ``Task``.

    The event's ``entity`` is the blocker itself — ``{"type": "blocker", "id": "BLK-0142"}``,
    the code minted by ``Blocker.save()`` — and ``payload.project_code`` names what it blocks,
    so a consumer aggregates by project without a foreign key into ``work`` (EVENTS.md §1).
    The audit row keeps the *project* code as ``entity_id``, because the project timeline is
    what renders it. The blocker's primary key is deliberately absent from the payload: a
    database id is exactly the foreign key into this context that EVENTS.md §1 forbids, and it
    is not in the documented schema. It stays in the audit ``metadata``, which never leaves
    this deployment.

    ``raised_at`` comes from the database default, since the column is ``auto_now_add`` by
    contract. ``command.now`` is domain time and is what the audit row and the event carry,
    so a replay reproduces the record even though it cannot reproduce the row timestamp.

    Args:
        command: Validated input. ``kind`` is already inside the closed set.

    Returns:
        The open blocker, with ``raised_at`` populated.

    Raises:
        ProjectNotFound: ``project_code`` does not resolve.
        TaskNotFound: ``task_code`` was given and does not resolve.
        TaskOutsideProject: The named task belongs to a different project.
        PersonNotFound: ``owner_code`` was given and is not on the roster.
    """
    project = Project.objects.filter(code=command.project_code).first()
    if project is None:
        raise ProjectNotFound(command.project_code)

    task = _resolve_task(command.task_code, project_id=project.pk, project_code=project.code)
    owner = _resolve_owner(command.owner_code)

    blocker = Blocker.objects.create(
        project=project,
        task=task,
        description=command.description,
        kind=command.kind.value,
        owner=owner,
    )

    write_activity(
        ActivityCommand(
            entity_type=ACTIVITY_ENTITY_BLOCKER,
            entity_id=project.code,
            verb="BLOCKER_RAISED",
            origin=ORIGIN_MANUAL,
            actor=command.actor,
            to_value="open",
            reason=command.description[:500],
            metadata={
                "blocker_id": blocker.pk,
                "kind": blocker.kind,
                "task_code": task.code if task else "",
                "owner": owner.code if owner else "",
            },
            occurred_at=command.now,
            correlation_id=command.correlation_id,
        )
    )
    enqueue_event(
        topic=TOPIC_BLOCKER_RAISED,
        entity_type=ENTITY_BLOCKER,
        entity_id=blocker.code,
        payload={
            "project_code": project.code,
            "kind": blocker.kind,
            "task_code": task.code if task else None,
            "owner_alias": owner.alias if owner else None,
            "description": blocker.description,
            "raised_at": blocker.raised_at.isoformat(),
        },
        actor=command.actor,
        correlation_id=command.correlation_id,
        occurred_at=command.now,
    )
    return blocker


def _resolve_task(task_code: str | None, *, project_id: int, project_code: str) -> Task | None:
    if task_code is None:
        return None
    task = Task.objects.for_code(task_code).first()
    if task is None:
        raise TaskNotFound(task_code)
    if task.project_id != project_id:
        raise TaskOutsideProject(task_code, project_code)
    return task


def _resolve_owner(owner_code: str | None) -> User | None:
    if owner_code is None:
        return None
    owner = User.objects.with_role().by_code(owner_code).first()
    if owner is None:
        raise PersonNotFound(owner_code)
    return owner
