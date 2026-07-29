"""End-to-end proofs for the Celery transport: delivery, deduplication and dead lettering.

``TransactionTestCase`` and not ``TestCase``, deliberately. The drain is kicked from
``transaction.on_commit``, and ``TestCase`` wraps each test in a transaction that never commits —
these tests would pass on it while proving nothing.

Celery runs eagerly here, so ``.delay()`` executes inline: the assertions are about the wiring
between the outbox, the registry and the ledger, not about the broker.
"""

from datetime import UTC, datetime

from django.db import transaction
from django.test import TransactionTestCase, override_settings

from apps.events.domain.envelope import (
    ENTITY_CLOCK,
    SYSTEM_ACTOR,
    TOPIC_CLOCK_TICKED,
)
from apps.events.models import OutboxEvent, ProcessedEvent
from apps.events.services import enqueue_event
from apps.events.tasks import OUTCOME_APPLIED, OUTCOME_DUPLICATE, drain_outbox, handle_event
from apps.events.tests.celery_support import EagerCeleryMixin
from apps.events.tests.registry_support import RecordingHandler, only_handlers

OCCURRED_AT = datetime(2026, 3, 1, 9, 0, tzinfo=UTC)


def _write_tick(**kwargs: object) -> OutboxEvent:
    """Write one ``clock.ticked`` row through the real write port, in its own transaction."""
    with transaction.atomic():
        envelope = enqueue_event(
            topic=TOPIC_CLOCK_TICKED,
            entity_type=ENTITY_CLOCK,
            entity_id="system",
            payload={"tick_at": OCCURRED_AT.isoformat(), "kind": "interval"},
            actor=SYSTEM_ACTOR,
            occurred_at=OCCURRED_AT,
            **kwargs,  # type: ignore[arg-type]
        )
    return OutboxEvent.objects.get(id=envelope.id)


class OutboxDeliveryTestCase(EagerCeleryMixin, TransactionTestCase):
    def test_an_outbox_row_reaches_every_handler_subscribed_to_its_topic(self) -> None:
        listener = RecordingHandler(name="listener", topics=frozenset({TOPIC_CLOCK_TICKED}))
        other = RecordingHandler(name="other", topics=frozenset({TOPIC_CLOCK_TICKED}))
        with only_handlers(listener.registration, other.registration):
            row = _write_tick()

        self.assertEqual([envelope.id for envelope in listener.seen], [row.id])
        self.assertEqual([envelope.id for envelope in other.seen], [row.id])
        row.refresh_from_db()
        self.assertIsNotNone(row.published_at)
        self.assertIsNone(row.dead_lettered_at)
        self.assertEqual(
            sorted(ProcessedEvent.objects.values_list("handler", flat=True)),
            ["listener", "other"],
        )

    def test_a_topic_nobody_subscribes_to_is_still_published(self) -> None:
        with only_handlers():
            row = _write_tick()

        row.refresh_from_db()
        self.assertIsNotNone(row.published_at)
        self.assertEqual(ProcessedEvent.objects.count(), 0)

    def test_the_drain_publishes_a_row_the_commit_kick_never_reached(self) -> None:
        """The Beat sweeper's path: a row left unpublished is picked up by a later drain."""
        listener = RecordingHandler(name="listener", topics=frozenset({TOPIC_CLOCK_TICKED}))
        with only_handlers():
            row = _write_tick()
        OutboxEvent.objects.filter(id=row.id).update(published_at=None)

        with only_handlers(listener.registration):
            dispatched = drain_outbox()

        # The return value is prose because it is what django_celery_results renders in the
        # admin, so assert on what it reports rather than on a bare count.
        self.assertIn("Dispatched 1 event(s)", dispatched)
        self.assertEqual([envelope.id for envelope in listener.seen], [row.id])


class IdempotencyTestCase(EagerCeleryMixin, TransactionTestCase):
    def test_the_same_event_delivered_twice_produces_one_effect(self) -> None:
        listener = RecordingHandler(name="listener", topics=frozenset({TOPIC_CLOCK_TICKED}))
        with only_handlers():
            row = _write_tick()

        with only_handlers(listener.registration):
            first = handle_event.apply(
                kwargs={"handler_name": "listener", "event_id": str(row.id)}
            ).get()
            second = handle_event.apply(
                kwargs={"handler_name": "listener", "event_id": str(row.id)}
            ).get()

        self.assertIn(OUTCOME_APPLIED, first)
        self.assertIn(OUTCOME_DUPLICATE, second)
        self.assertEqual(len(listener.seen), 1)
        self.assertEqual(ProcessedEvent.objects.filter(event_id=row.id).count(), 1)

    def test_two_handlers_on_one_event_each_apply_it_once(self) -> None:
        first = RecordingHandler(name="a-handler", topics=frozenset({TOPIC_CLOCK_TICKED}))
        second = RecordingHandler(name="b-handler", topics=frozenset({TOPIC_CLOCK_TICKED}))
        with only_handlers(first.registration, second.registration):
            row = _write_tick()
            drain_outbox()  # the redelivery a crashed drain would cause

        self.assertEqual(len(first.seen), 1)
        self.assertEqual(len(second.seen), 1)
        self.assertEqual(ProcessedEvent.objects.filter(event_id=row.id).count(), 2)


@override_settings(EVENT_MAX_ATTEMPTS=1)
class DeadLetterTestCase(EagerCeleryMixin, TransactionTestCase):
    def test_a_handler_past_the_attempt_budget_dead_letters_the_row(self) -> None:
        poisoned = RecordingHandler(
            name="poisoned",
            topics=frozenset({TOPIC_CLOCK_TICKED}),
            failure=RuntimeError("downstream is on fire"),
        )
        with only_handlers():
            row = _write_tick()

        with only_handlers(poisoned.registration), self.assertRaises(RuntimeError):
            handle_event.apply(kwargs={"handler_name": "poisoned", "event_id": str(row.id)}).get()

        row.refresh_from_db()
        self.assertIsNotNone(row.dead_lettered_at)
        self.assertEqual(row.attempts, 1)
        self.assertIn("downstream is on fire", row.last_error)
        self.assertFalse(ProcessedEvent.objects.filter(event_id=row.id).exists())

    def test_a_failed_handler_leaves_no_claim_so_a_requeue_is_a_real_retry(self) -> None:
        poisoned = RecordingHandler(
            name="poisoned",
            topics=frozenset({TOPIC_CLOCK_TICKED}),
            failure=RuntimeError("transient"),
        )
        with only_handlers():
            row = _write_tick()
        with only_handlers(poisoned.registration), self.assertRaises(RuntimeError):
            handle_event.apply(kwargs={"handler_name": "poisoned", "event_id": str(row.id)}).get()

        row.refresh_from_db()
        row.requeue()
        healed = RecordingHandler(name="poisoned", topics=frozenset({TOPIC_CLOCK_TICKED}))
        with only_handlers(healed.registration):
            drain_outbox()

        self.assertEqual([envelope.id for envelope in healed.seen], [row.id])
        row.refresh_from_db()
        self.assertIsNone(row.dead_lettered_at)
        self.assertIsNotNone(row.published_at)
