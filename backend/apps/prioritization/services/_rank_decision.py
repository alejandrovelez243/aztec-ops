"""Private helpers shared by the two use cases that let a human decide a rank.

Applying an override and revoking one are the same fact from the timeline's point of view — the
rank was decided by a person — so they write the same verb, the same origin and the same event.
Those three are defined once here rather than duplicated across the two modules, because a
revocation that published a differently-shaped event would be a revocation the queue does not
react to.

Private to ``services/``: nothing outside this package may import it.
"""

from datetime import datetime
from uuid import UUID

from apps.events.domain.envelope import (
    ENTITY_PROJECT,
    TOPIC_PROJECT_PRIORITY_RECALCULATED,
)
from apps.events.services import enqueue_event
from apps.portfolio.models import Project

from ..domain.errors import ProjectNotFound
from ..domain.views import ScoreView
from ..models import PriorityScore

#: ``ActivityRecord.origin`` and the event's ``origin`` for a rank a human forced. The audit trail
#: turns on this value: ``MANUAL`` means "somebody overrode the engine and owes an explanation",
#: which is why ``write_activity`` refuses a ``MANUAL`` record with a blank reason.
ORIGIN_MANUAL = "MANUAL"

#: Verb of every record these two use cases write, whether the override was applied or revoked.
#: One verb, because the timeline's "why did this move" filter must not miss half the answer.
VERB_PRIORITY_CHANGED = "PRIORITY_CHANGED"


def resolve_project_id(project_code: str) -> int:
    """Numeric id of the project, through the portfolio context's own named query.

    Args:
        project_code: The business code.

    Returns:
        The primary key every table in this context is keyed by.

    Raises:
        ProjectNotFound: No project carries that code.
    """
    project = Project.objects.by_code(project_code).first()
    if project is None:
        raise ProjectNotFound(project_code)
    return int(project.pk)


def publish_rank_decision(
    *,
    project_code: str,
    project_id: int,
    actor: str,
    correlation_id: UUID,
    now: datetime,
) -> None:
    """Emit ``project.priority.recalculated`` with ``origin: MANUAL``, inside the caller's atomic.

    The same topic the engine uses, on purpose: the queue reorders on it, and a second topic for
    "reordered by a human" would mean every consumer and the browser store learned two ways to
    react to one thing. ``origin`` is what tells them apart, and it is the field the timeline
    already keys on.

    ``value`` and ``previous_value`` are equal because an override never rewrites the computed
    score — that is the point of storing it separately. ``previous_value`` is ``null`` only when
    the engine has not yet run for this project.

    Args:
        project_code: Business code, carried as the envelope's ``entity.id``.
        project_id: Primary key, used to read the persisted score.
        actor: ``accounts.User.code`` of the person who decided the rank.
        correlation_id: Threaded from the API boundary.
        now: Domain time, the envelope's ``occurred_at``.
    """
    score = PriorityScore.objects.for_project(project_id).first()
    view = score.to_view() if score is not None else ScoreView(value=0.0, policy_version="")
    enqueue_event(
        topic=TOPIC_PROJECT_PRIORITY_RECALCULATED,
        entity_type=ENTITY_PROJECT,
        entity_id=project_code,
        payload={
            "value": view.value,
            "previous_value": view.value if score is not None else None,
            "policy_version": view.policy_version,
            "origin": ORIGIN_MANUAL,
            "breakdown": [signal.model_dump() for signal in view.breakdown],
            "modifiers": dict(view.modifiers),
            "flags": list(view.flags),
        },
        actor=actor,
        correlation_id=correlation_id,
        occurred_at=now,
    )
