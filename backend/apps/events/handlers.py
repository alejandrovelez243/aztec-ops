"""The ``sse-fanout`` handler: the bus's one door to the browser.

This is the single handler in the codebase permitted to hold a Redis client, and the reason is
that publishing *is* its effect. Everywhere else a ``PUBLISH`` beside a database write would be the
dual write ``apps.events.services`` exists to prevent; here there is no database write to be
inconsistent with — the handler's whole job is to forward a fact that has already committed and has
already been claimed by ``ProcessedEvent``.

The envelope is forwarded byte-for-byte. ``GET /api/stream`` frames it as ``id: <event.id>``,
``event: <topic>``, ``data: <envelope JSON>``, which is what makes ``es.addEventListener(topic, …)``
work in an island and what lets a reconnecting browser resume. Nothing here reshapes, filters or
enriches a payload: a fanout that edited what it forwarded would give the browser a third version
of a fact that the read model and the API already state.
"""

import logging

from django.conf import settings

from apps.events.domain.envelope import SSE_ALLOWLIST_TOPICS, EventEnvelope
from apps.events.redis_client import create_redis_client
from apps.events.registry import register_handler

logger = logging.getLogger(__name__)

#: The registered name, and therefore the ``handler`` half of every ``ProcessedEvent`` row this
#: handler writes. Renaming it replays the allowlist for the browser.
SSE_FANOUT_HANDLER = "sse-fanout"


@register_handler(name=SSE_FANOUT_HANDLER, topics=SSE_ALLOWLIST_TOPICS)
def publish_to_sse_channel(envelope: EventEnvelope) -> None:
    """Republish a browser-facing event onto ``settings.EVENT_SSE_CHANNEL``, unchanged.

    The subscription is the allowlist in ``apps.events.domain.envelope``, which is every topic
    except ``clock.ticked``. The tick is the standing counter-example and stays excluded on
    purpose: it renders nothing, a browser has its own clock, and forwarding it would push a frame
    every ``TICKER_INTERVAL_SECONDS`` to every open tab for the client to discard. What the tick
    *causes* — ``project.priority.recalculated`` — is on the allowlist and reaches the browser
    normally. There is no risk topic beside it: flags are computed on read (ADR 0011), so a client
    re-reads them with the project rather than being pushed a change it could not have missed.

    Delivery is at-least-once and this handler does not try to make it exactly-once. A duplicate
    delivery before the claim commits produces a duplicate frame, and that is the correct trade:
    the frame carries the same ``event.id``, so the client discards it, whereas publishing after
    the transaction closed would silently drop the frame whenever the publish failed with no
    transaction left to roll back and no retry left to take.

    Args:
        envelope: The delivered event, already filtered to the allowlist.

    Raises:
        redis.exceptions.RedisError: The publish failed. It is deliberately not caught: the
            enclosing transaction rolls back with the ``ProcessedEvent`` claim, so the task's next
            attempt is a real retry rather than a skip over an event the browser never saw.
    """
    receivers = create_redis_client().publish(settings.EVENT_SSE_CHANNEL, envelope.to_json())
    logger.debug(
        "event published to the SSE channel",
        extra={
            "event_id": str(envelope.id),
            "topic": envelope.topic,
            "channel": settings.EVENT_SSE_CHANNEL,
            "receivers": receivers,
        },
    )
