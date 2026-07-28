"""The outbox relay: the only process that turns committed rows into stream entries.

It claims unpublished rows with ``SELECT ... FOR UPDATE SKIP LOCKED``, reassembles the envelope,
``XADD``s it to ``settings.EVENT_STREAM`` and marks the row published. ``SKIP LOCKED`` is the
whole point: two relay processes take disjoint prefixes of the same ordered backlog without any
coordination, so the relay scales and restarts without a leader election or a lock table.

Delivery is at-least-once by construction. The relay can die after ``XADD`` and before writing
``published_at``, in which case the event is published twice; it can never be published zero
times, because the row is only claimed while the transaction that reads it holds the lock. Every
consumer therefore deduplicates on ``ProcessedEvent`` (EVENTS.md §6).
"""

import logging
import time
from typing import Final

from django.conf import settings
from django.db import transaction
from django.utils import timezone
from redis import Redis
from redis.exceptions import RedisError

from apps.events.domain.envelope import ENVELOPE_FIELD, EventEnvelope
from apps.events.models import OutboxEvent
from apps.events.redis_client import create_redis_client

logger = logging.getLogger(__name__)

#: Matches the LIMIT in the claim query documented in DATA_MODEL §10.3. Large enough that a burst
#: drains in a few round trips, small enough that one relay never holds the whole backlog locked.
DEFAULT_BATCH_SIZE: Final[int] = 100

#: How long the relay waits when the backlog is empty. This is the floor on end-to-end latency
#: from commit to browser, so it is sub-second on purpose.
DEFAULT_POLL_INTERVAL_SECONDS: Final[float] = 0.5

#: Written into ``stream_entry_id`` when a row was dead lettered rather than published, so the
#: table never claims an entry exists on ``aztec.events`` that does not.
DEAD_LETTER_ENTRY_PREFIX: Final[str] = "dlq-"

_ERROR_COLUMN_LIMIT: Final[int] = 500


def _entry_id_text(entry_id: object) -> str:
    """Render the id ``XADD`` returned as the text stored in ``stream_entry_id``.

    ``create_redis_client`` decodes responses, so this is already a ``str`` on every path this
    repository builds; redis-py's signature keeps ``bytes`` on the table for a client built
    without decoding. Formatting those directly is the failure mode this guards: the column would
    hold the literal ``b'1753-0'`` instead of ``1753-0``, and the id would no longer match
    anything in ``XRANGE``.

    Args:
        entry_id: Whatever ``XADD`` returned.

    Returns:
        The entry id as text, decoded when it arrived as bytes.
    """
    if isinstance(entry_id, bytes):
        return entry_id.decode()
    return str(entry_id)


class OutboxRelay:
    """Claims, publishes and marks outbox rows, batch by batch.

    Safe to run in several processes at once, and resumable: state lives entirely in the table,
    so a killed relay loses nothing and a restarted one continues from the oldest unpublished row.

    A row that cannot be published is not dropped and does not block the queue forever. Each
    failure increments ``attempts`` and records ``last_error``, both visible in the admin; past
    ``settings.EVENT_MAX_ATTEMPTS`` the envelope is written to ``settings.EVENT_DLQ_STREAM`` and
    the row is marked with a ``dlq-`` entry id, which retires it from the claim index while
    keeping the failure and the payload inspectable. If even the dead letter write fails the row
    stays unpublished and is retried, because losing the event is the one outcome not on offer.
    """

    def __init__(
        self,
        *,
        redis_client: Redis | None = None,
        stream: str | None = None,
        dlq_stream: str | None = None,
        max_attempts: int | None = None,
        batch_size: int = DEFAULT_BATCH_SIZE,
    ) -> None:
        self._redis = redis_client if redis_client is not None else create_redis_client()
        self._stream = stream if stream is not None else settings.EVENT_STREAM
        self._dlq_stream = dlq_stream if dlq_stream is not None else settings.EVENT_DLQ_STREAM
        self._max_attempts = (
            max_attempts if max_attempts is not None else settings.EVENT_MAX_ATTEMPTS
        )
        self._batch_size = batch_size

    def drain_once(self) -> list[EventEnvelope]:
        """Claim and publish at most one batch, then return what actually reached the stream.

        The claim, every ``XADD`` and every bookkeeping write happen in one transaction, so the
        rows stay locked for the whole publish. That is what stops a second relay from publishing
        the same row concurrently; it also means a crash mid-batch re-publishes the successful
        prefix on the next pass, which the consumers' deduplication absorbs.

        Returns:
            The envelopes published in this pass, in publication order. Empty when the backlog is
            empty or when every claimable row failed — a caller polling on the length is measuring
            progress, not health; health is ``attempts`` in the admin.
        """
        published: list[EventEnvelope] = []
        with transaction.atomic():
            rows = list(
                OutboxEvent.objects.select_for_update(skip_locked=True)
                .filter(published_at__isnull=True)
                .order_by("occurred_at", "id")[: self._batch_size]
            )
            for row in rows:
                envelope = self._publish(row)
                if envelope is not None:
                    published.append(envelope)
        return published

    def run_forever(
        self, poll_interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS
    ) -> None:  # pragma: no cover - the loop is exercised through drain_once
        """Drain continuously, sleeping only when there is nothing to claim.

        Runs until the process is signalled. It never sleeps after a full batch, so a burst drains
        at Redis speed rather than at one batch per poll interval.

        Args:
            poll_interval_seconds: Idle wait. Lower means lower commit-to-browser latency and more
                empty index scans; the index is partial on the unpublished set, so an empty scan
                is nearly free.
        """
        logger.info("relay started", extra={"stream": self._stream, "batch_size": self._batch_size})
        while True:
            published = self.drain_once()
            if not published:
                time.sleep(poll_interval_seconds)

    def _publish(self, row: OutboxEvent) -> EventEnvelope | None:
        """Publish one claimed row, or record why it could not be published."""
        envelope = row.to_envelope()
        try:
            entry_id = self._redis.xadd(self._stream, {ENVELOPE_FIELD: envelope.to_json()})
        except RedisError as error:
            self._record_failure(row, error)
            return None

        row.published_at = timezone.now()
        row.stream_entry_id = _entry_id_text(entry_id)
        row.last_error = ""
        row.save(update_fields=["published_at", "stream_entry_id", "last_error"])
        logger.debug(
            "event published",
            extra={
                "event_id": str(row.id),
                "topic": row.topic,
                "stream_entry_id": row.stream_entry_id,
            },
        )
        return envelope

    def _record_failure(self, row: OutboxEvent, error: RedisError) -> None:
        """Count the attempt, keep the cause, and dead letter once the budget is spent."""
        row.attempts += 1
        row.last_error = f"{type(error).__name__}: {error}"[:_ERROR_COLUMN_LIMIT]
        logger.warning(
            "relay could not publish event",
            extra={"event_id": str(row.id), "topic": row.topic, "attempts": row.attempts},
        )
        if row.attempts < self._max_attempts:
            row.save(update_fields=["attempts", "last_error"])
            return
        self._dead_letter(row)

    def _dead_letter(self, row: OutboxEvent) -> None:
        """Move an unpublishable row to the dead letter stream, or leave it for the next pass.

        The row is retired from the claim index only once the dead letter write has succeeded, so
        the failure mode of a Redis outage is a growing backlog — loud, visible in the admin and
        recoverable — rather than a silently discarded event.
        """
        envelope = row.to_envelope()
        try:
            entry_id = self._redis.xadd(
                self._dlq_stream,
                {
                    ENVELOPE_FIELD: envelope.to_json(),
                    "error": row.last_error,
                    "attempts": row.attempts,
                    "stage": "relay",
                },
            )
        except RedisError:
            logger.exception(
                "relay could not dead letter event; it stays unpublished and will be retried",
                extra={"event_id": str(row.id), "topic": row.topic},
            )
            row.save(update_fields=["attempts", "last_error"])
            return

        row.published_at = timezone.now()
        row.stream_entry_id = f"{DEAD_LETTER_ENTRY_PREFIX}{_entry_id_text(entry_id)}"[:32]
        logger.error(
            "event dead lettered by the relay",
            extra={
                "event_id": str(row.id),
                "topic": row.topic,
                "attempts": row.attempts,
                "dlq_stream": self._dlq_stream,
            },
        )
        row.save(update_fields=["attempts", "last_error", "published_at", "stream_entry_id"])
