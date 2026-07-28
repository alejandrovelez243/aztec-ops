"""Typed errors raised by the events context.

Every error carries the identifiers a log line or an admin page needs to act on it. The bus is
operated by a human reading `docs/RUNBOOK.md`, so an error whose message does not name the
event, the handler or the topic is an error that costs a database session to diagnose.
"""


class EventsError(Exception):
    """Base class for every failure originating in the events context.

    Callers catch this rather than :class:`Exception` when they need to distinguish a bus
    failure from a handler failure; nothing in this context raises a bare ``Exception``.
    """


class UnknownTopicError(EventsError):
    """A topic was used that `docs/EVENTS.md` §4 does not register.

    An undocumented topic does not exist (EVENTS.md §7.1): a consumer written against the catalog
    would never subscribe to it, so publishing one produces an event nobody will ever read.
    Raised at enqueue time, inside the caller's transaction, so the mutation rolls back with it.
    """

    def __init__(self, topic: str) -> None:
        super().__init__(f"Topic {topic!r} is not registered in docs/EVENTS.md section 4.")
        self.topic = topic


class EventNamesNoProjectError(EventsError):
    """An envelope reached a project-scoped consumer without naming a project.

    Every topic those handlers subscribe to either carries ``entity.type == "project"`` or a
    ``project_code`` in its payload (EVENTS.md §7.5) — that rule is what lets a consumer act on a
    task or a blocker without a foreign key into ``apps.work``. An envelope that satisfies neither
    is a producer bug, so it is raised rather than skipped: the delivery task retries it, dead-letters the
    outbox row and leaves it inspectable, whereas returning quietly would drop a real state change.
    """

    def __init__(self, topic: str, entity_type: str, entity_id: str) -> None:
        super().__init__(
            f"Event on topic {topic!r} for {entity_type}:{entity_id} names no project: "
            "entity.type is not 'project' and payload.project_code is absent or empty."
        )
        self.topic = topic
        self.entity_type = entity_type
        self.entity_id = entity_id


class MalformedTickError(EventsError):
    """A ``clock.ticked`` envelope carries no usable ``tick_at``.

    The consumers select what to recompute with ``tick_at`` and never with their own wall clock,
    so that a replayed tick lands on the same result. Falling back to ``now()`` would make a
    redelivery non-deterministic, which is exactly the property the field exists to protect.
    """

    def __init__(self, event_id: str, raw: object) -> None:
        super().__init__(f"clock.ticked event {event_id} carries no ISO-8601 tick_at: {raw!r}.")
        self.event_id = event_id
        self.raw = raw


class HandlerNotRegisteredError(EventsError):
    """No handler is registered under the name a delivery task carried.

    Two causes, and the message names both because they are fixed differently: the module holding
    the ``@register_handler`` function is not called ``handlers.py``, so app-ready never imported
    it; or the task was queued by a previous deploy for a handler that has since been deleted, in
    which case the event is already durable in the outbox and can be re-queued once a handler
    exists again.
    """

    def __init__(self, name: str, known_handlers: tuple[str, ...]) -> None:
        super().__init__(
            f"No handler registered under the name {name!r}. Registered handlers: "
            f"{', '.join(known_handlers) or '(none)'}."
        )
        self.name = name
        self.known_handlers = known_handlers
