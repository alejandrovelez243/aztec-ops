"""Test seams for the handler registry.

The registry is process-global by design — it is populated at app-ready from every context's
``handlers.py`` — so a test that wants to prove "this event reaches that handler" has to be able to
say which handlers exist while it runs. Doing that here rather than exposing a mutator on
``apps.events.registry`` keeps the production API to the four functions a handler author needs.
"""

from collections.abc import Iterator
from contextlib import contextmanager

from apps.events import registry
from apps.events.domain.envelope import EventEnvelope
from apps.events.registry import HandlerRegistration


class RecordingHandler:
    """A handler that remembers every envelope it was given, and can be told to fail."""

    def __init__(
        self, *, name: str, topics: frozenset[str], failure: Exception | None = None
    ) -> None:
        self.name = name
        self.topics = topics
        self.failure = failure
        self.seen: list[EventEnvelope] = []

    def __call__(self, envelope: EventEnvelope) -> None:
        """Record the delivery, then raise if this handler is configured to fail."""
        self.seen.append(envelope)
        if self.failure is not None:
            raise self.failure

    @property
    def registration(self) -> HandlerRegistration:
        """The registration the drain and the delivery task look up."""
        return HandlerRegistration(name=self.name, topics=self.topics, handle=self)


@contextmanager
def only_handlers(*registrations: HandlerRegistration) -> Iterator[None]:
    """Run the block with exactly these handlers registered, restoring the real ones after.

    Args:
        registrations: The handlers the bus should know about inside the block.

    Yields:
        Nothing. The point is the swapped registry, not a value.
    """
    original = dict(registry._REGISTRY)
    registry._REGISTRY.clear()
    registry._REGISTRY.update({item.name: item for item in registrations})
    try:
        yield
    finally:
        registry._REGISTRY.clear()
        registry._REGISTRY.update(original)
