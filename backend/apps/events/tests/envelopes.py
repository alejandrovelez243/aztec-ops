"""Envelope builders for the handler tests.

A handler's input is an :class:`~apps.events.domain.envelope.EventEnvelope`, never a service call,
so a test that wants to prove "this event produces that effect" has to construct one. These helpers
exist so the handler test modules do not each grow their own, drifting, idea of what a well-formed
envelope looks like.

Every builder takes ``occurred_at`` explicitly. Domain time is the handler's only clock, so a test
that let it default to ``now()`` could not tell a deterministic handler from one reading the wall
clock.
"""

from datetime import datetime
from uuid import UUID, uuid4

from pydantic import JsonValue

from apps.events.domain.envelope import (
    ENTITY_CLOCK,
    ENTITY_PROJECT,
    ENTITY_TASK,
    SYSTEM_ACTOR,
    TOPIC_CLOCK_TICKED,
    TOPIC_TASK_STATE_CHANGED,
    EntityRef,
    EventEnvelope,
)

TEST_ACTOR = "tester"


def project_event(
    *,
    topic: str,
    project_code: str,
    occurred_at: datetime,
    payload: dict[str, JsonValue] | None = None,
    event_id: UUID | None = None,
    correlation_id: UUID | None = None,
) -> EventEnvelope:
    """An envelope whose entity *is* the project, the shape every ``project.*`` topic carries."""
    identity = event_id if event_id is not None else uuid4()
    return EventEnvelope(
        id=identity,
        topic=topic,
        occurred_at=occurred_at,
        actor=TEST_ACTOR,
        correlation_id=correlation_id if correlation_id is not None else identity,
        entity=EntityRef(type=ENTITY_PROJECT, id=project_code),
        payload=payload if payload is not None else {},
    )


def task_event(
    *,
    project_code: str,
    task_code: str,
    occurred_at: datetime,
    topic: str = TOPIC_TASK_STATE_CHANGED,
    event_id: UUID | None = None,
) -> EventEnvelope:
    """An envelope about a task, carrying ``project_code`` the way EVENTS.md §7.5 requires.

    This is the shape that proves a consumer aggregates to the portfolio without a foreign key
    into ``apps.work``: the entity is a task code and nothing else in the envelope is.
    """
    identity = event_id if event_id is not None else uuid4()
    return EventEnvelope(
        id=identity,
        topic=topic,
        occurred_at=occurred_at,
        actor=TEST_ACTOR,
        correlation_id=identity,
        entity=EntityRef(type=ENTITY_TASK, id=task_code),
        payload={"project_code": project_code},
    )


def clock_tick(*, tick_at: datetime, event_id: UUID | None = None) -> EventEnvelope:
    """A ``clock.ticked`` envelope, which names no project at all.

    ``occurred_at`` and ``tick_at`` are deliberately the same instant here; they are separate
    fields because a redelivered tick keeps its ``tick_at`` while its delivery moves on, and the
    consumers must read the payload.
    """
    identity = event_id if event_id is not None else uuid4()
    return EventEnvelope(
        id=identity,
        topic=TOPIC_CLOCK_TICKED,
        occurred_at=tick_at,
        actor=SYSTEM_ACTOR,
        correlation_id=identity,
        entity=EntityRef(type=ENTITY_CLOCK, id="system"),
        payload={"tick_at": tick_at.isoformat(), "kind": "interval"},
    )
