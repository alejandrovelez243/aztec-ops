"""What the two engine handlers do when an event reaches them.

``TransactionTestCase``, because delivery goes through ``apps.events.tasks.apply_once`` and the
outbox rows these handlers write are committed by ``transaction.on_commit`` hooks that ``TestCase``
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
    TOPIC_PROJECT_PRIORITY_RECALCULATED,
    TOPIC_PROJECT_RISK_CHANGED,
    TOPIC_PROJECT_STATE_CHANGED,
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
from apps.prioritization.domain.types import Health
from apps.prioritization.handlers import (
    ENGINE_TOPICS,
    PRIORITY_RECALCULATOR,
    RISK_EVALUATOR,
    VERB_PRIORITY_CHANGED,
)
from apps.prioritization.models import PriorityScore, RiskFlag

OCCURRED_AT = datetime(2026, 3, 1, 9, 0, tzinfo=UTC)
LATER = OCCURRED_AT + timedelta(hours=1)
TICK_AT = OCCURRED_AT + timedelta(days=1)


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

    def test_neither_engine_consumes_what_an_engine_emits(self) -> None:
        for topic in (TOPIC_PROJECT_PRIORITY_RECALCULATED, TOPIC_PROJECT_RISK_CHANGED):
            with self.subTest(topic=topic):
                subscribed = {registration.name for registration in handlers_for(topic)}
                self.assertNotIn(PRIORITY_RECALCULATOR, subscribed)
                self.assertNotIn(RISK_EVALUATOR, subscribed)

    def test_both_engines_read_every_write_side_topic_and_the_clock(self) -> None:
        expected = ALL_TOPICS - {TOPIC_PROJECT_PRIORITY_RECALCULATED, TOPIC_PROJECT_RISK_CHANGED}
        self.assertEqual(ENGINE_TOPICS, expected)
        self.assertIn(TOPIC_CLOCK_TICKED, ENGINE_TOPICS)


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

    def test_a_tick_uses_the_payload_instant_and_not_the_wall_clock(self) -> None:
        self._expire()

        _deliver(RISK_EVALUATOR, clock_tick(tick_at=TICK_AT))

        raised = RiskFlag.objects.for_project(self.scenario.project.pk).open().oldest_first()
        self.assertTrue(raised)
        self.assertTrue(all(flag.detected_at == TICK_AT for flag in raised))


class RiskEvaluatorTestCase(EagerCeleryMixin, TransactionTestCase):
    """Reacting on the risk side: rewrite the flags, derive health, announce only a change."""

    def setUp(self) -> None:
        super().setUp()
        self.scenario = PortfolioScenario()
        self.scenario.block_project()

    def test_a_blocked_project_raises_its_flags_and_announces_the_health_it_reached(self) -> None:
        envelope = project_event(
            topic=TOPIC_PROJECT_STATE_CHANGED,
            project_code=PROJECT_CODE,
            occurred_at=OCCURRED_AT,
            payload={"from": "execution", "to": "blocked"},
        )

        _deliver(RISK_EVALUATOR, envelope)

        emitted = OutboxEvent.objects.get(topic=TOPIC_PROJECT_RISK_CHANGED)
        self.assertEqual(emitted.actor, SYSTEM_ACTOR)
        self.assertEqual(emitted.correlation_id, envelope.correlation_id)
        self.assertEqual(emitted.payload["health"], Health.BLOCKED.value)
        self.assertIn("BLOCKED", emitted.payload["added"])
        self.assertEqual(
            {flag["code"] for flag in emitted.payload["flags"]},
            set(
                RiskFlag.objects.for_project(self.scenario.project.pk)
                .open()
                .values_list("code", flat=True)
            ),
        )

    def test_an_unchanged_evaluation_announces_nothing(self) -> None:
        first = project_event(
            topic=TOPIC_PROJECT_STATE_CHANGED, project_code=PROJECT_CODE, occurred_at=OCCURRED_AT
        )
        _deliver(RISK_EVALUATOR, first)

        second = project_event(
            topic=TOPIC_PROJECT_STATE_CHANGED, project_code=PROJECT_CODE, occurred_at=LATER
        )
        _deliver(RISK_EVALUATOR, second)

        self.assertEqual(OutboxEvent.objects.filter(topic=TOPIC_PROJECT_RISK_CHANGED).count(), 1)

    def test_the_risk_engine_writes_no_activity_record(self) -> None:
        _deliver(
            RISK_EVALUATOR,
            project_event(
                topic=TOPIC_PROJECT_STATE_CHANGED,
                project_code=PROJECT_CODE,
                occurred_at=OCCURRED_AT,
            ),
        )

        self.assertFalse(
            ActivityRecord.objects.filter(entity_id=PROJECT_CODE).exists(),
            "the RiskFlag rows are the risk history; the verb set has no risk verb",
        )
