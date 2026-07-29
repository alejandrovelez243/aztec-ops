"""Two questions every project-scoped handler asks of an envelope, answered once.

``priority-recalculator`` and ``snapshot-builder`` both begin the same way: *which project is this
about*, and — for ``clock.ticked`` — *which instant does this tick represent*. A second copy of
either answer is a second place for "``entity.id`` when the entity is a project,
``payload.project_code`` otherwise" to drift, and it is where a handler starts reading its own wall
clock instead of the tick.

Both helpers are pure functions over the envelope: no Django, no database, no clock, so they are
provable on ``SimpleTestCase``. They live beside the envelope rather than inside it because they
read *inside* the payload, which the envelope contract keeps deliberately opaque — the catalog
must stay decodable after a topic is retired.
"""

from datetime import datetime

from apps.events.domain.envelope import ENTITY_PROJECT, EventEnvelope
from apps.events.domain.errors import EventNamesNoProjectError, MalformedTickError

#: Payload key every non-project topic carries so a handler can aggregate to the portfolio
#: without a foreign key into ``apps.work`` (EVENTS.md §7.5).
PROJECT_CODE_FIELD = "project_code"

#: Payload key of the instant a ``clock.ticked`` event represents (EVENTS.md §4).
TICK_AT_FIELD = "tick_at"


def project_code_of(envelope: EventEnvelope) -> str:
    """The business code of the project an event is about.

    Args:
        envelope: A delivered event on a project-scoped topic.

    Returns:
        ``entity.id`` when the event is about a project, otherwise ``payload.project_code`` —
        the code a task, blocker or note event carries precisely so its reader never joins into
        the emitting context.

    Raises:
        EventNamesNoProjectError: Neither source is present. This is a producer bug rather than a
            delivery problem, and raising is what dead-letters the outbox row, where it stays
            visible instead of being silently dropped.
    """
    if envelope.entity.type == ENTITY_PROJECT:
        return envelope.entity.id

    code = envelope.payload.get(PROJECT_CODE_FIELD)
    if not isinstance(code, str) or not code:
        raise EventNamesNoProjectError(envelope.topic, envelope.entity.type, envelope.entity.id)
    return code


def tick_at_of(envelope: EventEnvelope) -> datetime:
    """The instant a ``clock.ticked`` event represents.

    Read from the payload and never from ``timezone.now()``: the selection of stale scores and
    every age derived from it must reproduce exactly when the same tick is redelivered, which a
    wall clock cannot do.

    Args:
        envelope: A ``clock.ticked`` envelope.

    Returns:
        The aware instant, normalized by ``datetime.fromisoformat`` (``Z`` included).

    Raises:
        MalformedTickError: ``tick_at`` is absent, is not a string, is not ISO-8601, or is naive.
            A tick with no usable instant cannot be applied deterministically, so it fails loudly.
    """
    raw = envelope.payload.get(TICK_AT_FIELD)
    if not isinstance(raw, str):
        raise MalformedTickError(str(envelope.id), raw)
    try:
        moment = datetime.fromisoformat(raw)
    except ValueError as error:
        raise MalformedTickError(str(envelope.id), raw) from error
    if moment.tzinfo is None:
        raise MalformedTickError(str(envelope.id), raw)
    return moment
