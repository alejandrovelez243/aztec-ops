"""The handler registry: what a reactor may declare, and what it may not."""

from django.test import SimpleTestCase

from apps.events.domain.envelope import (
    TOPIC_CLOCK_TICKED,
    TOPIC_PROJECT_STATE_CHANGED,
    EventEnvelope,
)
from apps.events.domain.errors import HandlerNotRegisteredError
from apps.events.registry import get_handler, handlers_for, register_handler
from apps.events.tests.registry_support import RecordingHandler, only_handlers


def _noop(_envelope: EventEnvelope) -> None:
    return None


class HandlerRegistrationTestCase(SimpleTestCase):
    def test_a_handler_is_dispatched_only_for_the_topics_it_declared(self) -> None:
        listener = RecordingHandler(name="listener", topics=frozenset({TOPIC_CLOCK_TICKED}))
        with only_handlers(listener.registration):
            self.assertEqual([item.name for item in handlers_for(TOPIC_CLOCK_TICKED)], ["listener"])
            self.assertEqual(handlers_for(TOPIC_PROJECT_STATE_CHANGED), ())

    def test_two_handlers_on_one_topic_are_dispatched_in_name_order(self) -> None:
        second = RecordingHandler(name="b-handler", topics=frozenset({TOPIC_CLOCK_TICKED}))
        first = RecordingHandler(name="a-handler", topics=frozenset({TOPIC_CLOCK_TICKED}))
        with only_handlers(second.registration, first.registration):
            self.assertEqual(
                [item.name for item in handlers_for(TOPIC_CLOCK_TICKED)],
                ["a-handler", "b-handler"],
            )

    def test_an_unregistered_topic_is_refused_at_import_time(self) -> None:
        with only_handlers(), self.assertRaises(ValueError):
            register_handler(name="typo", topics={"project.state-changed"})(_noop)

    def test_two_handlers_cannot_share_a_name(self) -> None:
        with only_handlers():
            register_handler(name="taken", topics={TOPIC_CLOCK_TICKED})(_noop)
            with self.assertRaises(ValueError):
                register_handler(name="taken", topics={TOPIC_CLOCK_TICKED})(lambda _e: None)

    def test_an_empty_subscription_is_refused(self) -> None:
        with only_handlers(), self.assertRaises(ValueError):
            register_handler(name="silent", topics=set())(_noop)

    def test_resolving_an_unknown_handler_names_the_ones_that_exist(self) -> None:
        listener = RecordingHandler(name="listener", topics=frozenset({TOPIC_CLOCK_TICKED}))
        with only_handlers(listener.registration):
            with self.assertRaises(HandlerNotRegisteredError) as caught:
                get_handler("gone")
            self.assertIn("listener", str(caught.exception))
