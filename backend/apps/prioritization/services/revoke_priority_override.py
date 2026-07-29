"""Use case: take a manual override back out of force.

Revocation is a write of ``revoked_at``, never a delete. The row is the evidence that somebody
forced a rank and why; deleting it would make the queue quietly correct itself with nothing left
to explain the fortnight it spent wrong. Because the computed ``PriorityScore`` was never
overwritten, removing the override restores the engine's ranking with no recomputation at all —
which is the property the whole override design exists to keep (ARCHITECTURE §4.2).
"""

from datetime import datetime
from uuid import UUID

from django.db import transaction

from apps.activity.domain.value_objects import ActivityCommand
from apps.activity.services import write_activity
from apps.events.domain.envelope import ENTITY_PROJECT

from ..models import PriorityOverride
from ._rank_decision import (
    ORIGIN_MANUAL,
    VERB_PRIORITY_CHANGED,
    publish_rank_decision,
    resolve_project_id,
)

#: Recorded as the reason when an override is lifted. ``write_activity`` refuses a ``MANUAL``
#: record with a blank reason, and "the operator did not type anything" is not a licence to write
#: an unexplained rank change into the trail — so the act itself is the explanation.
REVOCATION_REASON = "Manual priority override revoked; the computed ranking applies again."


@transaction.atomic
def revoke_priority_override(
    *,
    project_code: str,
    actor: str,
    correlation_id: UUID,
    now: datetime,
) -> None:
    """Revoke the override in force on a project, if there is one.

    Revoking when nothing is in force is a no-op that writes no record and emits no event, so
    ``DELETE`` stays idempotent: a client retrying after a dropped response gets the same 204 and
    the timeline does not grow a second "reverted" entry for one revert.

    Args:
        project_code: The business code of the project.
        actor: ``accounts.User.code`` of the person lifting the override.
        correlation_id: Threaded from the API boundary.
        now: Domain time, written as the revocation instant.

    Raises:
        ProjectNotFound: No project carries that code.
    """
    project_id = resolve_project_id(project_code)
    override = PriorityOverride.objects.for_project(project_id).live().first()
    if override is None:
        return

    override.revoked_at = now
    override.save(update_fields=["revoked_at"])

    write_activity(
        ActivityCommand(
            entity_type=ENTITY_PROJECT,
            entity_id=project_code,
            verb=VERB_PRIORITY_CHANGED,
            origin=ORIGIN_MANUAL,
            actor=actor,
            reason=REVOCATION_REASON,
            metadata={
                "origin": ORIGIN_MANUAL,
                "override_id": override.pk,
                "revoked": True,
            },
            occurred_at=now,
            correlation_id=correlation_id,
        )
    )
    publish_rank_decision(
        project_code=project_code,
        project_id=project_id,
        actor=actor,
        correlation_id=correlation_id,
        now=now,
    )
