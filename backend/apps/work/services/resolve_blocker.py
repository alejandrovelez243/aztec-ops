"""Use case: close an open blocker, stating how it was cleared."""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from django.db import transaction

from apps.activity.domain.value_objects import ActivityCommand
from apps.activity.services import write_activity
from apps.events.domain.envelope import ENTITY_BLOCKER, TOPIC_BLOCKER_RESOLVED
from apps.events.services import enqueue_event
from apps.work.domain.errors import (
    BlockerAlreadyResolved,
    BlockerNotFound,
    ResolutionReasonRequired,
)
from apps.work.models import Blocker
from apps.work.services import ORIGIN_MANUAL

if TYPE_CHECKING:
    from apps.work.domain.commands import ResolveBlockerCommand

#: ``ActivityRecord.entity_type`` for a fact about a blocker; ``entity_id`` is the project code.
ACTIVITY_ENTITY_BLOCKER: Final = "blocker"


@transaction.atomic
def resolve_blocker(command: ResolveBlockerCommand) -> Blocker:
    """Set ``resolved_at`` and the resolution reason on a blocker that is still open.

    The blocker is read under a row lock, which is what makes "already resolved" a real
    rejection rather than a race: without it two concurrent calls both see an open row, and
    the second silently overwrites the first resolver's reason and re-emits the event.

    The reason is demanded here as well as by the database check, so the operator gets a
    message rather than an integrity error. A blocker closed without one is
    indistinguishable, weeks later, from an impediment that was never real — which is how
    the same blocker comes back with nobody able to say what was tried.

    The event's ``entity`` is the same ``{"type": "blocker", "id": "BLK-0142"}`` that
    ``blocker.raised`` carried: the code is minted once on insert and never recomputed, so the
    two events about one impediment are joinable by ``entity.id`` alone, with no primary key in
    the payload — a database id is the foreign key into this context EVENTS.md §1 forbids. The
    audit row still names the project, which is where the fact is read.

    Clearing the project's ``BLOCKED`` risk flag is not done here, or anywhere. The flag is a
    function of the open blocker rows, so resolving the last one clears it by definition the next
    time anybody reads the project (ADR 0011) — there is no derived row to reconcile and nothing
    to get out of step with this write.

    Args:
        command: Validated input naming the blocker and the resolution reason.

    Returns:
        The resolved blocker.

    Raises:
        BlockerNotFound: No blocker carries that primary key.
        BlockerAlreadyResolved: It was closed before this call reached it.
        ResolutionReasonRequired: The reason was whitespace only.
    """
    blocker = Blocker.objects.locked().with_relations().filter(pk=command.blocker_id).first()
    if blocker is None:
        raise BlockerNotFound(command.blocker_id)
    if blocker.resolved_at is not None:
        raise BlockerAlreadyResolved(command.blocker_id)
    if not command.reason.strip():
        raise ResolutionReasonRequired(command.blocker_id)

    blocker.resolved_at = command.now
    blocker.resolution_reason = command.reason
    blocker.save(update_fields=["resolved_at", "resolution_reason"])

    write_activity(
        ActivityCommand(
            entity_type=ACTIVITY_ENTITY_BLOCKER,
            entity_id=blocker.project.code,
            verb="BLOCKER_RESOLVED",
            origin=ORIGIN_MANUAL,
            actor=command.actor,
            from_value="open",
            to_value="resolved",
            reason=command.reason,
            metadata={
                "blocker_id": blocker.pk,
                "kind": blocker.kind,
                "task_code": blocker.task.code if blocker.task else "",
            },
            occurred_at=command.now,
            correlation_id=command.correlation_id,
        )
    )
    enqueue_event(
        topic=TOPIC_BLOCKER_RESOLVED,
        entity_type=ENTITY_BLOCKER,
        entity_id=blocker.code,
        payload={
            "project_code": blocker.project.code,
            "kind": blocker.kind,
            "task_code": blocker.task.code if blocker.task else None,
            "resolution": command.reason,
            "resolved_at": command.now.isoformat(),
            "open_for_days": (command.now - blocker.raised_at).days,
        },
        actor=command.actor,
        correlation_id=command.correlation_id,
        occurred_at=command.now,
    )
    return blocker
