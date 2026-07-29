"""What ``read_pipeline_health`` reports, and why each number is the one an operator needs.

``TestCase`` and not ``TransactionTestCase``: this is a read. Nothing here depends on ``on_commit``
or on a committed transaction, so the cheaper base class proves exactly as much.

Rows are written directly on the model rather than through ``enqueue_event``, on purpose. The write
port registers an ``on_commit`` drain kick, and a health read must be assertable against a backlog
that nothing is draining — which is precisely the situation the endpoint exists to make visible.
"""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from django.test import TestCase

from apps.events.domain.envelope import (
    ENTITY_CLOCK,
    ENTITY_PROJECT,
    SYSTEM_ACTOR,
    TOPIC_CLOCK_TICKED,
    TOPIC_PROJECT_STATE_CHANGED,
)
from apps.events.models import OutboxEvent
from apps.events.services import read_pipeline_health

NOW = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)


def _row(*, topic: str, occurred_at: datetime, **fields: object) -> OutboxEvent:
    """One outbox row, written past the write port so no drain is scheduled."""
    entity_type = ENTITY_CLOCK if topic == TOPIC_CLOCK_TICKED else ENTITY_PROJECT
    return OutboxEvent.objects.create(
        id=uuid4(),
        topic=topic,
        entity_type=entity_type,
        entity_id="PRJ-T1",
        payload={},
        actor=SYSTEM_ACTOR,
        correlation_id=uuid4(),
        occurred_at=occurred_at,
        **fields,
    )


class PipelineHealthTestCase(TestCase):
    def test_an_empty_outbox_reports_no_backlog_and_no_tick(self) -> None:
        health = read_pipeline_health(now=NOW)

        self.assertEqual(health.unpublished, 0)
        self.assertEqual(health.dead_lettered, 0)
        self.assertIsNone(health.oldest_unpublished_age_seconds)
        self.assertIsNone(health.last_tick_at)

    def test_the_backlog_is_counted_with_the_age_of_its_oldest_row(self) -> None:
        _row(topic=TOPIC_PROJECT_STATE_CHANGED, occurred_at=NOW - timedelta(seconds=90))
        _row(topic=TOPIC_PROJECT_STATE_CHANGED, occurred_at=NOW - timedelta(seconds=30))

        health = read_pipeline_health(now=NOW)

        self.assertEqual(health.unpublished, 2)
        self.assertEqual(health.oldest_unpublished_age_seconds, 90.0)

    def test_a_published_row_leaves_the_backlog(self) -> None:
        _row(
            topic=TOPIC_PROJECT_STATE_CHANGED,
            occurred_at=NOW - timedelta(seconds=10),
            published_at=NOW,
        )

        health = read_pipeline_health(now=NOW)

        self.assertEqual(health.unpublished, 0)
        self.assertIsNone(health.oldest_unpublished_age_seconds)

    def test_a_dead_lettered_row_is_counted_separately_from_the_backlog(self) -> None:
        _row(
            topic=TOPIC_PROJECT_STATE_CHANGED,
            occurred_at=NOW - timedelta(minutes=5),
            published_at=NOW,
            dead_lettered_at=NOW,
            attempts=5,
            last_error="RuntimeError: boom",
        )

        health = read_pipeline_health(now=NOW)

        self.assertEqual(health.dead_lettered, 1)
        self.assertEqual(health.unpublished, 0)

    def test_the_last_tick_is_the_newest_clock_row_and_carries_its_age(self) -> None:
        _row(topic=TOPIC_CLOCK_TICKED, occurred_at=NOW - timedelta(minutes=30))
        _row(topic=TOPIC_CLOCK_TICKED, occurred_at=NOW - timedelta(minutes=2))
        _row(topic=TOPIC_PROJECT_STATE_CHANGED, occurred_at=NOW)

        health = read_pipeline_health(now=NOW)

        self.assertEqual(health.last_tick_at, NOW - timedelta(minutes=2))
        self.assertEqual(health.last_tick_age_seconds, 120.0)

    def test_a_row_from_the_future_reports_a_zero_age_and_never_a_negative_one(self) -> None:
        _row(topic=TOPIC_CLOCK_TICKED, occurred_at=NOW + timedelta(seconds=5))

        health = read_pipeline_health(now=NOW)

        self.assertEqual(health.last_tick_age_seconds, 0.0)
