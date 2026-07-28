"""Use case: a human forces a project's rank, and the whole system learns why.

:func:`~apps.prioritization.services.apply_override.apply_override` writes the override row and
deliberately nothing else — it does not touch the audit trail or the outbox, because the caller
owns those and a scoring change must never roll back the movement that caused it. This module is
that caller for the HTTP boundary: it wraps the row write with the ``ActivityRecord`` CLAUDE.md
rule 3 requires and the ``OutboxEvent`` rule 4 requires, in one transaction.

The computed ``PriorityScore`` is not read for the decision and never written. That is the
invariant the whole override design protects: the engine's number stays visible beside the forced
position, so the queue can say "a human moved this" and a revocation restores the ranking with no
recomputation (ARCHITECTURE §4.2).
"""

from datetime import datetime
from uuid import UUID

from django.db import transaction

from apps.activity.domain.value_objects import ActivityCommand
from apps.activity.services import write_activity
from apps.events.domain.envelope import ENTITY_PROJECT

from ._rank_decision import (
    ORIGIN_MANUAL,
    VERB_PRIORITY_CHANGED,
    publish_rank_decision,
    resolve_project_id,
)
from .apply_override import ApplyOverrideCommand, apply_override
from .read_project_priority import ProjectPriorityView, read_project_priority


@transaction.atomic
def override_priority(
    *,
    command: ApplyOverrideCommand,
    correlation_id: UUID,
    now: datetime,
) -> ProjectPriorityView:
    """Put a manual override in force, audit it and publish it, in one transaction.

    Args:
        command: The validated intent, carrying the mandatory reason and exactly one of
            ``position`` / ``boost``.
        correlation_id: Threaded from the API boundary, so this record and the records of any
            project the move displaced reconstruct as one decision.
        now: Domain time. Also the instant the replaced override is revoked at.

    Returns:
        The project's prioritization state after the override: the **unchanged** computed score,
        the override now in force, and the open risk flags.

    Raises:
        OverrideReasonRequired: The reason is empty or whitespace only.
        OverrideMechanismAmbiguous: Neither or both of ``position`` and ``boost`` were given.
        ProjectNotFound: The command names no existing project.
    """
    result = apply_override(command=command, now=now)
    project_id = resolve_project_id(command.project_code)

    write_activity(
        ActivityCommand(
            entity_type=ENTITY_PROJECT,
            entity_id=command.project_code,
            verb=VERB_PRIORITY_CHANGED,
            origin=ORIGIN_MANUAL,
            actor=command.actor,
            reason=command.reason,
            metadata={
                "origin": ORIGIN_MANUAL,
                "override_id": result.override_id,
                "replaced_override_id": result.replaced_override_id,
                "position": result.position,
                "boost": float(result.boost) if result.boost is not None else None,
            },
            occurred_at=now,
            correlation_id=correlation_id,
        )
    )
    publish_rank_decision(
        project_code=command.project_code,
        project_id=project_id,
        actor=command.actor,
        correlation_id=correlation_id,
        now=now,
    )
    return read_project_priority(project_id=project_id, now=now)
