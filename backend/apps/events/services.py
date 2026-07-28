"""The outbox write port: the one function every other context calls to publish an event.

This module deliberately does not import Redis, and must never start to. That is not a style
preference — a service is code that runs inside ``transaction.atomic()``, and an ``XADD`` cannot
be rolled back with the transaction that issued it. A later ``IntegrityError`` would then leave a
published event describing a change that never happened. Writing a row instead makes the event
exactly as atomic as the mutation; delivery becomes the relay's problem, and the relay retries.

Usage from a calling context, inside its own transaction:

    from apps.events.domain.envelope import ENTITY_PROJECT, TOPIC_PROJECT_STATE_CHANGED
    from apps.events.services import enqueue_event

    @transaction.atomic
    def transition_project(...) -> Project:
        ...
        write_activity(...)
        enqueue_event(
            topic=TOPIC_PROJECT_STATE_CHANGED,
            entity_type=ENTITY_PROJECT,
            entity_id=project.code,
            payload=payload_model.model_dump(mode="json"),
            actor=actor,
            correlation_id=correlation_id,
            occurred_at=now,
        )
"""

import logging
from collections.abc import Mapping
from datetime import datetime
from uuid import UUID, uuid4

from pydantic import JsonValue

from apps.events.domain.envelope import (
    INITIAL_VERSION,
    EntityRef,
    EventEnvelope,
    is_registered_topic,
)
from apps.events.domain.errors import UnknownTopicError
from apps.events.models import OutboxEvent

logger = logging.getLogger(__name__)


# PLR0913: the envelope's fields are fixed by EVENTS.md §1. Folding them into a container object
# would hide which of them are required at the call site — the one place a forgotten actor or
# correlation_id is still cheap to notice.
def enqueue_event(  # noqa: PLR0913
    *,
    topic: str,
    entity_type: str,
    entity_id: str,
    payload: Mapping[str, JsonValue],
    actor: str,
    occurred_at: datetime,
    correlation_id: UUID | None = None,
    version: int = INITIAL_VERSION,
) -> EventEnvelope:
    """Append one event to the outbox, inside the caller's already-open transaction.

    The caller must already be in ``transaction.atomic()`` — this function opens none of its own,
    on purpose: the whole guarantee of the pattern is that the event commits with the aggregate
    mutation and the ``ActivityRecord``, and a nested transaction here would let the three come
    apart. Nothing is published; the relay does that, later, from another process.

    The envelope is built and validated *before* the row is written, so a payload that cannot
    round-trip through JSON fails here, inside the producing transaction, rather than at XADD time
    when the mutation is already committed and unrecoverable.

    Args:
        topic: A topic registered in `docs/EVENTS.md` §4. Use the ``TOPIC_*`` constants from
            ``apps.events.domain.envelope`` rather than a literal.
        entity_type: ``project`` | ``task`` | ``blocker`` | ``note`` | ``clock``.
        entity_id: The **business code** (``PRJ-01``, ``PRJ-01-T02``), never a primary key. A
            consumer in another context must never need a foreign key into yours.
        payload: The topic's documented body, already JSON-shaped (dates as ISO strings, decimals
            as numbers). Build it as a Pydantic model in your context's ``domain/events.py`` and
            pass ``model.model_dump(mode="json")``.
        actor: ``accounts.User.code`` from the request, or ``"system"`` when an engine caused it.
            Never empty — an unattributed change is a bug.
        occurred_at: When the change committed, in domain time. Passed in rather than read from
            the clock so a service stays deterministic and a replay does not invent a new instant.
        correlation_id: The decision this event belongs to. Thread the request's id through so
            "deprioritize A in order to prioritize B" reconstructs as one movement. Defaults to
            the new event's own id, which is correct for an event that starts a decision.
        version: Payload schema version *of this topic*. Bump only for a breaking change
            (EVENTS.md §2).

    Returns:
        The envelope exactly as the relay will publish it. Its ``id`` is the deduplication key
        every consumer group will use and the ``id:`` of the SSE frame, so a caller that needs to
        correlate its own logging with the bus reads it from here.

    Raises:
        UnknownTopicError: The topic is not in the EVENTS.md §4 catalog. An undocumented topic
            has no payload schema and no subscriber, so publishing one produces an event nobody
            will ever read.
        pydantic.ValidationError: The envelope is malformed — a naive ``occurred_at``, a payload
            value that is not JSON, an empty ``actor``, or a ``version`` below 1.
    """
    if not is_registered_topic(topic):
        raise UnknownTopicError(topic)

    event_id = uuid4()
    envelope = EventEnvelope(
        id=event_id,
        topic=topic,
        occurred_at=occurred_at,
        actor=actor,
        correlation_id=correlation_id if correlation_id is not None else event_id,
        entity=EntityRef(type=entity_type, id=entity_id),
        payload=dict(payload),
        version=version,
    )

    OutboxEvent.objects.create(
        id=envelope.id,
        topic=envelope.topic,
        entity_type=envelope.entity.type,
        entity_id=envelope.entity.id,
        payload=envelope.payload,
        actor=envelope.actor,
        correlation_id=envelope.correlation_id,
        version=envelope.version,
        occurred_at=envelope.occurred_at,
    )
    logger.debug(
        "outbox row written",
        extra={
            "event_id": str(envelope.id),
            "topic": envelope.topic,
            "entity_id": envelope.entity.id,
        },
    )
    return envelope
