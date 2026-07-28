"""The reactor that keeps the command center's read model true: ``snapshot-builder``.

``ProjectSnapshot`` is the answer to the one question the command center asks — project, score,
flags, health, owner load, task counts and open blockers, ranked — and computing it per request is
a six-table join with per-row aggregates (ARCHITECTURE §8). This handler is what pays for it once
per event instead of once per page load.

It **emits nothing**, which is what makes it safe to subscribe to everything, including the two
derived topics. A handler that both consumed ``project.priority.recalculated`` and emitted an event
would close a cycle on the bus; this one ends the chain.

The rebuild is wholesale, never a patch of the column the event happened to name. A partial rebuild
would leave one row holding columns from two different deliveries while ``last_event_id`` claimed a
single one, and that is the kind of staleness nobody can diagnose afterwards.
"""

import logging
from typing import Final

from apps.events.domain.envelope import (
    ALL_TOPICS,
    TOPIC_CLOCK_TICKED,
    EventEnvelope,
)
from apps.events.domain.routing import project_code_of
from apps.events.registry import register_handler
from apps.portfolio.services import rebuild_snapshot

logger = logging.getLogger(__name__)

#: Registered name, and the ``handler`` half of every ``ProcessedEvent`` row this handler writes.
SNAPSHOT_BUILDER = "snapshot-builder"

#: Every topic that names a project — which is every topic except the clock.
#:
#: Derived by subtraction rather than listed, on purpose: a new topic is on the read model's
#: subscription the moment it is registered, because the failure mode of forgetting it is a
#: silently stale command center rather than an error anyone would see. ``clock.ticked`` names no
#: project at all and is excluded structurally: what a tick *causes* —
#: ``project.priority.recalculated``, ``project.risk.changed`` — arrives here as its own event, so
#: nothing is missed by ignoring the tick itself.
SNAPSHOT_TOPICS: Final[frozenset[str]] = ALL_TOPICS - {TOPIC_CLOCK_TICKED}


@register_handler(name=SNAPSHOT_BUILDER, topics=SNAPSHOT_TOPICS)
def rebuild_project_snapshot(envelope: EventEnvelope) -> None:
    """Rebuild the read-model row of the project this event is about.

    The project is ``entity.id`` when the event is about a project and ``payload.project_code``
    otherwise, so a task, blocker or note event aggregates to the portfolio without a foreign key
    into the emitting context.

    ``occurred_at`` is the clock for the whole rebuild — "overdue", blocker ages, days since last
    activity. Using the wall clock instead would let a redelivery write a row that quietly
    disagrees with the score computed from the same event.

    Args:
        envelope: A delivered event on one of :data:`SNAPSHOT_TOPICS`.

    Raises:
        EventNamesNoProjectError: A non-project event carries no ``project_code``. A producer bug;
            it dead-letters the row so the read model's gap is visible rather than assumed.
        ProjectNotFound: The event names a project that no longer exists. Retrying will not fix it,
            and the outbox row is where that stays inspectable.
    """
    project_code = project_code_of(envelope)
    rebuild_snapshot(project_code=project_code, now=envelope.occurred_at, last_event_id=envelope.id)
    logger.debug(
        "project snapshot rebuilt",
        extra={
            "event_id": str(envelope.id),
            "topic": envelope.topic,
            "project_code": project_code,
        },
    )
