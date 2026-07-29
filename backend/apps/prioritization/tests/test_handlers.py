"""What the ranking engine handler does when an event reaches it.

``TransactionTestCase``, because delivery goes through ``apps.events.tasks.apply_once`` and the
outbox rows this handler writes are committed by ``transaction.on_commit`` hooks that ``TestCase``
never runs — such a test would pass while proving nothing about the outbox.

Every delivery goes through ``apply_once`` and the *real* registration looked up by name, rather
than by calling the handler function directly. That way the tests also prove the handler is
registered under the name ``ProcessedEvent`` will store, and they exercise the claim, so
"redelivery produces one effect" is a real assertion instead of a description.

The registry is narrowed to the handler under test for the duration of each delivery: committing an
outbox row kicks the drain, which runs inline here, and a full registry would fan the emitted event
out to ``sse-fanout`` — a handler whose entire job is to reach Redis.
"""

from datetime import UTC, datetime, timedelta

from django.test import SimpleTestCase, TransactionTestCase

from apps.activity.models import ActivityRecord
from apps.events.domain.envelope import (
    ALL_TOPICS,
    SYSTEM_ACTOR,
    TOPIC_CLOCK_TICKED,
    TOPIC_MEMBER_ACTIVATION_CHANGED,
    TOPIC_MEMBER_CREATED,
    TOPIC_MEMBER_UPDATED,
    TOPIC_PROJECT_PRIORITY_RECALCULATED,
    TOPIC_PROJECT_UPDATED,
    EventEnvelope,
)
from apps.events.models import OutboxEvent
from apps.events.registry import get_handler, handlers_for
from apps.events.tasks import apply_once
from apps.events.tests.celery_support import EagerCeleryMixin
from apps.events.tests.envelopes import clock_tick, project_event, task_event
from apps.events.tests.registry_support import only_handlers
from apps.portfolio.tests.scenario import PROJECT_CODE, SECOND_PROJECT_CODE, PortfolioScenario
from apps.prioritization.handlers import (
    ENGINE_TOPICS,
    PRIORITY_RECALCULATOR,
    VERB_PRIORITY_CHANGED,
)
from apps.prioritization.models import PriorityScore

OCCURRED_AT = datetime(2026, 3, 1, 9, 0, tzinfo=UTC)
LATER = OCCURRED_AT + timedelta(hours=1)
TICK_AT = OCCURRED_AT + timedelta(days=1)

#: The roster's own topics, named here so the subscription assertions read as a statement about
#: the engine rather than as a list somebody has to keep in step with the catalog by hand.
MEMBER_TOPICS = frozenset(
    {TOPIC_MEMBER_CREATED, TOPIC_MEMBER_UPDATED, TOPIC_MEMBER_ACTIVATION_CHANGED}
)


def _deliver(handler_name: str, envelope: EventEnvelope) -> bool:
    """Deliver one event to one registered handler, exactly as the delivery task would.

    Args:
        handler_name: The registered name, so a renamed handler fails the test loudly.
        envelope: The event to apply.

    Returns:
        True when this delivery applied the effect, False when the claim already existed.
    """
    registration = get_handler(handler_name)
    with only_handlers(registration):
        return apply_once(registration, envelope)


class EngineSubscriptionTestCase(SimpleTestCase):
    """The shape of the subscription, which is what keeps the bus acyclic.

    ``SimpleTestCase``: the registry is pure Python, and proving that no database is needed here is
    part of the point.
    """

    def test_the_engine_does_not_consume_what_it_emits(self) -> None:
        subscribed = {
            registration.name for registration in handlers_for(TOPIC_PROJECT_PRIORITY_RECALCULATED)
        }
        self.assertNotIn(PRIORITY_RECALCULATOR, subscribed)

    def test_the_engine_reads_every_topic_about_the_work_and_the_clock(self) -> None:
        # Everything the bus carries, minus what the engine emits and minus the roster: a person's
        # capacity feeds ``OWNER_OVERLOADED``, which is computed on read (ADR 0011), so there is no
        # score for a roster edit to move and nothing for a recomputation to persist.
        expected = ALL_TOPICS - {TOPIC_PROJECT_PRIORITY_RECALCULATED} - MEMBER_TOPICS
        self.assertEqual(ENGINE_TOPICS, expected)
        self.assertIn(TOPIC_CLOCK_TICKED, ENGINE_TOPICS)

    def test_a_roster_edit_never_reaches_the_ranking(self) -> None:
        # Stated as its own assertion rather than left implicit in the set arithmetic above,
        # because the failure it guards against is silent: an engine subscribed to the roster
        # rescores every project an edited person owns, and arrives at the same numbers.
        for topic in MEMBER_TOPICS:
            with self.subTest(topic=topic):
                subscribed = {registration.name for registration in handlers_for(topic)}
                self.assertNotIn(PRIORITY_RECALCULATOR, subscribed)

    def test_no_topic_announces_a_risk_change(self) -> None:
        """Flags are computed on read, so there is no moment at which they change (ADR 0011)."""
        self.assertNotIn("project.risk.changed", ALL_TOPICS)


class PriorityRecalculatorTestCase(EagerCeleryMixin, TransactionTestCase):
    """Reacting to a data event: score, audit, and announce only a real movement."""

    def setUp(self) -> None:
        super().setUp()
        self.scenario = PortfolioScenario()

    def test_a_task_event_scores_the_project_it_names_in_its_payload(self) -> None:
        envelope = task_event(
            project_code=PROJECT_CODE, task_code="PRJ-T1-T1", occurred_at=OCCURRED_AT
        )

        self.assertTrue(_deliver(PRIORITY_RECALCULATOR, envelope))

        score = PriorityScore.objects.for_project(self.scenario.project.pk).first()
        self.assertIsNotNone(score)
        assert score is not None
        self.assertEqual(score.computed_at, OCCURRED_AT)

    def test_a_first_score_is_announced_with_the_breakdown_that_justifies_it(self) -> None:
        envelope = project_event(
            topic=TOPIC_PROJECT_UPDATED,
            project_code=PROJECT_CODE,
            occurred_at=OCCURRED_AT,
            payload={"changes": {"next_step": {"from": None, "to": "Ship it"}}},
        )

        _deliver(PRIORITY_RECALCULATOR, envelope)

        emitted = OutboxEvent.objects.get(topic=TOPIC_PROJECT_PRIORITY_RECALCULATED)
        self.assertEqual(emitted.entity_id, PROJECT_CODE)
        self.assertEqual(emitted.actor, SYSTEM_ACTOR)
        self.assertEqual(emitted.correlation_id, envelope.correlation_id)
        self.assertIsNone(emitted.payload["previous_value"])
        self.assertEqual(
            {line["code"] for line in emitted.payload["breakdown"]},
            set(self.scenario.policy.weights),
        )

    def test_a_movement_is_audited_as_policy_with_the_signal_that_carried_it(self) -> None:
        envelope = task_event(
            project_code=PROJECT_CODE, task_code="PRJ-T1-T1", occurred_at=OCCURRED_AT
        )

        _deliver(PRIORITY_RECALCULATOR, envelope)

        record = ActivityRecord.objects.get(entity_id=PROJECT_CODE, verb=VERB_PRIORITY_CHANGED)
        self.assertEqual(record.origin, ActivityRecord.Origin.POLICY)
        self.assertEqual(record.actor, SYSTEM_ACTOR)
        self.assertEqual(record.correlation_id, envelope.correlation_id)
        self.assertIn(record.metadata["signal"], self.scenario.policy.weights)
        self.assertEqual(record.metadata["trigger_topic"], envelope.topic)

    def test_a_second_event_that_changes_nothing_announces_nothing(self) -> None:
        first = task_event(
            project_code=PROJECT_CODE, task_code="PRJ-T1-T1", occurred_at=OCCURRED_AT
        )
        _deliver(PRIORITY_RECALCULATOR, first)

        second = task_event(
            project_code=PROJECT_CODE, task_code="PRJ-T1-T1", occurred_at=OCCURRED_AT
        )
        _deliver(PRIORITY_RECALCULATOR, second)

        self.assertEqual(
            OutboxEvent.objects.filter(topic=TOPIC_PROJECT_PRIORITY_RECALCULATED).count(), 1
        )
        self.assertEqual(
            ActivityRecord.objects.filter(
                entity_id=PROJECT_CODE, verb=VERB_PRIORITY_CHANGED
            ).count(),
            1,
        )

    def test_the_same_event_delivered_twice_produces_one_effect(self) -> None:
        envelope = task_event(
            project_code=PROJECT_CODE, task_code="PRJ-T1-T1", occurred_at=OCCURRED_AT
        )

        self.assertTrue(_deliver(PRIORITY_RECALCULATOR, envelope))
        self.assertFalse(_deliver(PRIORITY_RECALCULATOR, envelope))

        self.assertEqual(
            OutboxEvent.objects.filter(topic=TOPIC_PROJECT_PRIORITY_RECALCULATED).count(), 1
        )


class ClockTickTestCase(EagerCeleryMixin, TransactionTestCase):
    """Reacting to time: only the scores that expired, and silence when none did."""

    def setUp(self) -> None:
        super().setUp()
        self.scenario = PortfolioScenario()
        _deliver(
            PRIORITY_RECALCULATOR,
            task_event(project_code=PROJECT_CODE, task_code="PRJ-T1-T1", occurred_at=OCCURRED_AT),
        )
        OutboxEvent.objects.all().delete()

    def _expire(self) -> None:
        """Make the primary project's score due, and give it something new to say."""
        PriorityScore.objects.for_project(self.scenario.project.pk).update(
            valid_until=TICK_AT - timedelta(seconds=1)
        )
        self.scenario.block_project()

    def test_a_tick_recomputes_only_the_scores_whose_validity_expired(self) -> None:
        self._expire()

        _deliver(PRIORITY_RECALCULATOR, clock_tick(tick_at=TICK_AT))

        emitted = OutboxEvent.objects.get(topic=TOPIC_PROJECT_PRIORITY_RECALCULATED)
        self.assertEqual(emitted.entity_id, PROJECT_CODE)
        recomputed = PriorityScore.objects.for_project(self.scenario.project.pk).get()
        self.assertEqual(recomputed.computed_at, TICK_AT)
        self.assertFalse(
            PriorityScore.objects.filter(project__code=SECOND_PROJECT_CODE).exists(),
            "a project with no score has nothing to expire and must stay untouched",
        )

    def test_a_quiet_tick_recomputes_nothing_and_emits_nothing(self) -> None:
        PriorityScore.objects.for_project(self.scenario.project.pk).update(
            valid_until=TICK_AT + timedelta(days=7)
        )

        _deliver(PRIORITY_RECALCULATOR, clock_tick(tick_at=TICK_AT))

        self.assertEqual(OutboxEvent.objects.count(), 0)
        self.assertEqual(
            PriorityScore.objects.for_project(self.scenario.project.pk).get().computed_at,
            OCCURRED_AT,
        )

    def test_a_tick_scores_against_the_payload_instant_and_not_the_wall_clock(self) -> None:
        self._expire()

        _deliver(PRIORITY_RECALCULATOR, clock_tick(tick_at=TICK_AT))

        self.assertEqual(
            PriorityScore.objects.for_project(self.scenario.project.pk).get().computed_at,
            TICK_AT,
        )
