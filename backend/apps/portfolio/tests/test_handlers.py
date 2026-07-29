"""What ``snapshot-builder`` does when an event reaches it.

``TransactionTestCase`` because delivery goes through ``apps.events.tasks.apply_once``, whose claim
and effect share a transaction that has to actually commit for "delivered twice, rebuilt once" to
mean anything.

The read model is the one thing in the system nobody notices is wrong: a snapshot that stopped
being rebuilt renders a plausible, stale command center. So the assertions here are about the row
existing and carrying the delivery that produced it, not only about the handler returning.
"""

from datetime import UTC, datetime, timedelta

from django.test import SimpleTestCase, TransactionTestCase

from apps.events.domain.envelope import (
    TOPIC_CLOCK_TICKED,
    TOPIC_PROJECT_PRIORITY_RECALCULATED,
    TOPIC_PROJECT_STATE_CHANGED,
    EventEnvelope,
)
from apps.events.models import OutboxEvent
from apps.events.registry import get_handler, handlers_for
from apps.events.tasks import apply_once
from apps.events.tests.celery_support import EagerCeleryMixin
from apps.events.tests.envelopes import project_event, task_event
from apps.events.tests.registry_support import only_handlers
from apps.portfolio.handlers import SNAPSHOT_BUILDER, SNAPSHOT_TOPICS
from apps.portfolio.models import ProjectSnapshot
from apps.portfolio.tests.scenario import PROJECT_CODE, PortfolioScenario

OCCURRED_AT = datetime(2026, 3, 1, 9, 0, tzinfo=UTC)
LATER = OCCURRED_AT + timedelta(hours=2)


def _deliver(envelope: EventEnvelope) -> bool:
    """Deliver one event to the registered snapshot builder, as the delivery task would."""
    registration = get_handler(SNAPSHOT_BUILDER)
    with only_handlers(registration):
        return apply_once(registration, envelope)


class SnapshotSubscriptionTestCase(SimpleTestCase):
    """The subscription: everything that names a project, and nothing that does not."""

    def test_the_read_model_is_rebuilt_from_the_derived_topic_too(self) -> None:
        self.assertIn(TOPIC_PROJECT_PRIORITY_RECALCULATED, SNAPSHOT_TOPICS)
        self.assertIn(
            SNAPSHOT_BUILDER,
            {
                registration.name
                for registration in handlers_for(TOPIC_PROJECT_PRIORITY_RECALCULATED)
            },
        )

    def test_the_clock_is_not_subscribed_because_it_names_no_project(self) -> None:
        self.assertNotIn(TOPIC_CLOCK_TICKED, SNAPSHOT_TOPICS)
        self.assertNotIn(
            SNAPSHOT_BUILDER,
            {registration.name for registration in handlers_for(TOPIC_CLOCK_TICKED)},
        )


class SnapshotBuilderTestCase(EagerCeleryMixin, TransactionTestCase):
    """Rebuilding the command center's row, and staying silent while doing it."""

    def setUp(self) -> None:
        super().setUp()
        self.scenario = PortfolioScenario()

    def test_a_task_event_rebuilds_the_project_named_in_its_payload(self) -> None:
        envelope = task_event(
            project_code=PROJECT_CODE, task_code="PRJ-T1-T1", occurred_at=OCCURRED_AT
        )

        self.assertTrue(_deliver(envelope))

        snapshot = ProjectSnapshot.objects.get(project_code=PROJECT_CODE)
        self.assertEqual(snapshot.last_event_id, envelope.id)
        self.assertEqual(snapshot.state_code, self.scenario.in_progress.code)
        self.assertEqual(snapshot.owner_code, self.scenario.owner.code)

    def test_a_later_event_replaces_the_row_wholesale(self) -> None:
        _deliver(task_event(project_code=PROJECT_CODE, task_code="T1", occurred_at=OCCURRED_AT))
        self.scenario.block_project()

        second = project_event(
            topic=TOPIC_PROJECT_STATE_CHANGED, project_code=PROJECT_CODE, occurred_at=LATER
        )
        _deliver(second)

        snapshot = ProjectSnapshot.objects.get(project_code=PROJECT_CODE)
        self.assertEqual(snapshot.state_code, self.scenario.blocked.code)
        self.assertEqual(snapshot.last_event_id, second.id)
        self.assertEqual(ProjectSnapshot.objects.count(), 1)

    def test_the_snapshot_builder_emits_nothing(self) -> None:
        _deliver(task_event(project_code=PROJECT_CODE, task_code="T1", occurred_at=OCCURRED_AT))

        self.assertEqual(
            OutboxEvent.objects.count(),
            0,
            "the read model ends the chain; an event here would close a cycle on the bus",
        )

    def test_the_same_event_delivered_twice_rebuilds_once(self) -> None:
        envelope = task_event(project_code=PROJECT_CODE, task_code="T1", occurred_at=OCCURRED_AT)

        self.assertTrue(_deliver(envelope))
        self.assertFalse(_deliver(envelope))

        self.assertEqual(ProjectSnapshot.objects.count(), 1)
