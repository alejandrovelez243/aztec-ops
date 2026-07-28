"""Use case: move a project to another workflow state through a declared transition."""

from datetime import datetime
from uuid import UUID

from django.db import transaction

from apps.activity.domain.value_objects import ActivityCommand
from apps.activity.services import write_activity
from apps.events.domain.envelope import ENTITY_PROJECT, TOPIC_PROJECT_STATE_CHANGED
from apps.events.services import enqueue_event
from apps.portfolio.domain.errors import ProjectNotFound
from apps.portfolio.domain.value_objects import ProjectStateChange
from apps.portfolio.repositories import ProjectRepository
from apps.workflow.services.transition import validate_transition

#: ``ActivityRecord.origin`` for a move a human justified, and for one they did not. The audit
#: trail distinguishes them so a reader can tell an argued decision from a routine step; a MANUAL
#: record with no reason is rejected by the activity service, which is the rule this encodes.
_ORIGIN_JUSTIFIED = "MANUAL"
_ORIGIN_ROUTINE = "SYSTEM"


@transaction.atomic
def transition_project(
    *,
    project_code: str,
    to_state_code: str,
    actor: str,
    reason: str,
    correlation_id: UUID,
    now: datetime,
) -> ProjectStateChange:
    """Move a project along a declared edge, then record that it moved — in one transaction.

    The legality of the move is not decided here. ``apps.workflow`` owns the graph and validates
    the edge, the required reason, the required fields and the guard, returning a ``TransitionCheck``;
    this service performs the assignment its own aggregate owns. That split is why the workflow
    context never imports ``Project`` and why nothing below names a state ``code`` or ``category``:
    adding ``en_espera_cliente`` is a fixture row, not a deploy.

    This is the only writer of ``Project.workflow_state``. Everything it writes — the column, the
    ``ActivityRecord`` with verb ``STATE_CHANGED`` and the ``OutboxEvent`` with topic
    ``project.state_changed`` — commits together or not at all, so the trail can never claim a move
    that rolled back and the event can never describe one that did not happen. Redis is untouched:
    the relay publishes the committed row, and ``priority-recalculator``, ``risk-evaluator`` and
    ``sse-fanout`` react independently, so a slow rescore cannot fail a legitimate move.

    Args:
        project_code: Business code, e.g. "PRJ-01".
        to_state_code: ``WorkflowState.code`` inside the workflow the project is bound to.
        actor: ``accounts.User.code``, or "system" when the engine caused the change.
        reason: Free text. Required only when the traversed edge sets ``requires_reason``; the
            workflow service, not this one, decides that. A non-empty reason also marks the record
            as a justified decision rather than a routine step.
        correlation_id: Threaded from the API boundary so a chained decision — deprioritize A in
            order to prioritize B — reconstructs as one movement.
        now: Domain time, supplied by the caller so a replay does not invent a new instant.

    Returns:
        The new projection plus the edge that was traversed, so the caller can render "moved from X
        to Y" without re-reading the previous state from the audit trail.

    Raises:
        ProjectNotFound: No project carries ``project_code``.
        TransitionNotAllowed: No active edge from the current state to ``to_state_code``.
        ReasonRequired: The edge sets ``requires_reason`` and ``reason`` is blank.
        RequiredFieldMissing: A field named in ``requires_fields`` is empty, e.g. ``next_step``.
        GuardRejected: The edge's registered guard refused the move.
    """
    repository = ProjectRepository()
    project = repository.get_for_update(project_code)
    if project is None:
        raise ProjectNotFound(project_code)

    check = validate_transition(
        entity=project,
        to_state_code=to_state_code,
        actor=actor,
        now=now,
        reason=reason,
    )

    project.workflow_state_id = check.to_state_id
    project.save(update_fields=["workflow_state", "updated_at"])

    write_activity(
        ActivityCommand(
            entity_type=ENTITY_PROJECT,
            entity_id=project.code,
            verb="STATE_CHANGED",
            origin=_ORIGIN_JUSTIFIED if reason.strip() else _ORIGIN_ROUTINE,
            actor=actor,
            from_value=check.from_state_code,
            to_value=check.to_state_code,
            reason=reason,
            metadata={
                "transition_id": check.transition_id,
                "to_category": check.to_state_category,
                "guard": check.guard,
            },
            occurred_at=now,
            correlation_id=correlation_id,
        )
    )
    enqueue_event(
        topic=TOPIC_PROJECT_STATE_CHANGED,
        entity_type=ENTITY_PROJECT,
        entity_id=project.code,
        payload={
            "from": check.from_state_code,
            "to": check.to_state_code,
            "from_category": check.from_state_category,
            "to_category": check.to_state_category,
            "transition": check.edge_code,
            "reason": reason or None,
        },
        actor=actor,
        correlation_id=correlation_id,
        occurred_at=now,
    )

    # Re-read so the returned projection carries the new state's label and category rather than the
    # stale related object still cached on the instance from before the assignment.
    moved = repository.get_by_code(project_code)
    if moved is None:  # pragma: no cover - the row is locked inside this transaction
        raise ProjectNotFound(project_code)

    return ProjectStateChange(
        project=moved.to_result(),
        from_state_code=check.from_state_code,
        to_state_code=check.to_state_code,
        reason=reason,
    )
