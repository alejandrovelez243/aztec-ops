"""Typed errors raised by the events context.

Every error carries the identifiers a log line or an admin page needs to act on it. The bus is
operated by a human reading `docs/RUNBOOK.md`, so an error whose message does not name the
event, the group or the topic is an error that costs a database session to diagnose.
"""


class EventsError(Exception):
    """Base class for every failure originating in the events context.

    Callers catch this rather than :class:`Exception` when they need to distinguish a bus
    failure from a handler failure; nothing in this context raises a bare ``Exception``.
    """


class EnvelopeDecodeError(EventsError):
    """A stream entry could not be turned back into an :class:`EventEnvelope`.

    The invariant broken is that everything on ``aztec.events`` was written by the relay from a
    validated envelope. Raising instead of skipping is deliberate: a silently dropped entry is
    indistinguishable from an empty stream, so the runner routes the raw entry to the dead letter
    stream and the entry stays inspectable.
    """

    def __init__(self, entry_id: str, reason: str) -> None:
        super().__init__(f"Stream entry {entry_id} is not a valid event envelope: {reason}")
        self.entry_id = entry_id
        self.reason = reason


class UnknownTopicError(EventsError):
    """A topic was used that `docs/EVENTS.md` §4 does not register.

    An undocumented topic does not exist (EVENTS.md §7.1): a consumer written against the catalog
    would never subscribe to it, so publishing one produces an event nobody will ever read.
    Raised at enqueue time, inside the caller's transaction, so the mutation rolls back with it.
    """

    def __init__(self, topic: str) -> None:
        super().__init__(f"Topic {topic!r} is not registered in docs/EVENTS.md section 4.")
        self.topic = topic


class ConsumerGroupNotRegisteredError(EventsError):
    """No consumer class is registered under the requested group name.

    Almost always means the module holding the ``@register_consumer`` class was never imported:
    consumers are discovered from ``apps/<context>/consumers/__init__.py`` at app-ready time.
    """

    def __init__(self, group: str, known_groups: tuple[str, ...]) -> None:
        super().__init__(
            f"No consumer registered for group {group!r}. Registered groups: "
            f"{', '.join(known_groups) or '(none)'}."
        )
        self.group = group
        self.known_groups = known_groups
