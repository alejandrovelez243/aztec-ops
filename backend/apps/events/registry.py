"""The handler registry: the one place that maps a topic to the reactors subscribed to it.

This is the seam CLAUDE.md rule 8 protects. A producing service writes an outbox row and names a
topic; it never names a handler. Adding a reactor is therefore a decorated function and nothing
else — no producer changes, no ``if`` grows, no compose service appears.

**Declaring a handler.** In ``apps/<context>/handlers.py`` (the module name matters: app-ready
autodiscovers exactly that name, so a handler in a module called anything else is never imported
and silently never runs)::

    from apps.events.domain.envelope import TOPIC_CLOCK_TICKED, TOPIC_PROJECT_STATE_CHANGED
    from apps.events.registry import register_handler

    @register_handler(name="priority-recalculator", topics={TOPIC_PROJECT_STATE_CHANGED, TOPIC_CLOCK_TICKED})
    def recalculate_priorities(envelope: EventEnvelope) -> None:
        ...

**What a handler may assume.**

* It runs inside an open ``transaction.atomic()`` that already contains the ``ProcessedEvent``
  claim for ``(envelope.id, name)``. Everything it writes commits with that claim or not at all,
  which is what makes a retry a real retry instead of a silent skip.
* It is never called twice for the same ``envelope.id`` under the same handler name *after a
  success*. It can absolutely be called twice after a failure. Deduplication is per handler.
* ``envelope.topic`` is always in its declared ``topics``; the drain dispatches nothing else, so
  widening the subscription later replays nothing.
* Time comes from ``envelope.occurred_at`` (or the tick payload), never from the wall clock. A
  redelivery must land on the same result.

**What a handler must not do.** Do not catch its own failures to "keep things moving" — raising is
how the retry, the log line and the dead letter happen, and a swallowed exception is an event that
vanished. Do not call ``transaction.commit`` or open a second connection; breaking the enclosing
transaction breaks deduplication.

The registry is pure Python: no Django model, no Celery, no Redis, so a registration can be
asserted on ``SimpleTestCase``.
"""

from collections.abc import Callable, Iterable

from pydantic import BaseModel, ConfigDict, Field

from apps.events.domain.envelope import EventEnvelope, is_registered_topic
from apps.events.domain.errors import HandlerNotRegisteredError

#: What a reactor is: a function of one envelope with no return value. A function rather than a
#: class because there is exactly one method to implement, and a class would only add a name for
#: the state it is forbidden to keep.
type EventHandler = Callable[[EventEnvelope], None]

#: Upper bound on a handler name, matching ``ProcessedEvent.handler``. A name that does not fit the
#: column would fail at claim time, inside the handler's transaction, instead of at import.
HANDLER_NAME_MAX_LENGTH = 48


class HandlerRegistration(BaseModel):
    """One reactor, its name and its subscription.

    ``name`` is the durable half of the idempotency key ``(event_id, handler)`` and is written into
    every ``ProcessedEvent`` row, so renaming a deployed handler replays history for it. That is
    occasionally what you want and never what you want by accident — treat the name as released.
    """

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    name: str = Field(min_length=1, max_length=HANDLER_NAME_MAX_LENGTH)
    topics: frozenset[str] = Field(min_length=1)
    handle: EventHandler


_REGISTRY: dict[str, HandlerRegistration] = {}


def register_handler(*, name: str, topics: Iterable[str]) -> Callable[[EventHandler], EventHandler]:
    """Register a function as the reactor called ``name`` for every topic in ``topics``.

    Validation happens at import time on purpose: an unknown topic or a duplicated name is a
    wiring mistake, and a wiring mistake that only surfaces when the event finally fires is a
    mistake found in production.

    Args:
        name: The handler name. It becomes the ``handler`` half of the idempotency key and the
            ``handler_name`` argument of the ``events.handle_event`` task, so it must be stable
            across deploys and unique across the codebase.
        topics: The subscription, from the ``TOPIC_*`` constants in
            ``apps.events.domain.envelope``. Never a literal — a typo in a literal is a handler
            that silently never runs.

    Returns:
        The decorator, which returns the function unchanged so the module can still call it
        directly and a test can import it without going through the bus.

    Raises:
        ValueError: The subscription is empty, names a topic outside the EVENTS.md §4 catalog, or
            the name is already taken by a different function. A silently overwritten registration
            would deploy one of two reactors at random.
        pydantic.ValidationError: The name is empty or longer than the ``ProcessedEvent.handler``
            column.
    """

    def decorate(handle: EventHandler) -> EventHandler:
        registration = HandlerRegistration(name=name, topics=frozenset(topics), handle=handle)
        unknown = sorted(topic for topic in registration.topics if not is_registered_topic(topic))
        if unknown:
            message = (
                f"Handler {name!r} subscribes to unregistered topic(s) {', '.join(unknown)}; "
                "every topic must appear in the docs/EVENTS.md section 4 catalog."
            )
            raise ValueError(message)
        existing = _REGISTRY.get(name)
        if existing is not None and existing.handle is not handle:
            message = (
                f"Handler name {name!r} is already registered by "
                f"{existing.handle.__module__}.{existing.handle.__qualname__}."
            )
            raise ValueError(message)
        _REGISTRY[name] = registration
        return handle

    return decorate


def handlers_for(topic: str) -> tuple[HandlerRegistration, ...]:
    """Every handler subscribed to a topic, in a deterministic order.

    Sorted by name so two workers dispatch the same event in the same order and a log of a
    redelivery is comparable with the original. Handlers do not depend on each other — the order
    is for readability, not for correctness.

    Args:
        topic: The topic of an event about to be dispatched.

    Returns:
        The matching registrations, possibly empty. An empty result is normal: a topic nobody
        reacts to is still published, still stored and still visible in the admin.
    """
    return tuple(
        sorted(
            (registration for registration in _REGISTRY.values() if topic in registration.topics),
            key=lambda registration: registration.name,
        )
    )


def get_handler(name: str) -> HandlerRegistration:
    """Resolve a handler name to its registration.

    Args:
        name: The name carried by an ``events.handle_event`` task.

    Returns:
        The registration, whose ``handle`` is called inside the claim's transaction.

    Raises:
        HandlerNotRegisteredError: Nothing registered that name. Almost always a handler module
            that is not called ``handlers.py``, so app-ready never imported it — or a task queued
            by a previous deploy for a handler that has since been deleted.
    """
    registration = _REGISTRY.get(name)
    if registration is None:
        raise HandlerNotRegisteredError(name, registered_handlers())
    return registration


def registered_handlers() -> tuple[str, ...]:
    """Every registered handler name, sorted, for error messages and operational checks."""
    return tuple(sorted(_REGISTRY))
