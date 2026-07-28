"""The consumer-group loop, the idempotency wrapper and the dead letter path.

Three consumer groups written elsewhere in the codebase plug into this module, so the contract
below is precise on purpose.

**Writing a consumer.** Subclass :class:`EventConsumer` in your own context, declare ``group`` and
``topics``, implement ``handle``, and decorate the class with :func:`register_consumer`::

    # apps/prioritization/consumers/priority_recalculator.py
    @register_consumer
    class PriorityRecalculator(EventConsumer):
        group = "priority-recalculator"
        topics = frozenset({TOPIC_PROJECT_STATE_CHANGED, TOPIC_CLOCK_TICKED, ...})

        def handle(self, envelope: EventEnvelope) -> None:
            ...

    # apps/prioritization/consumers/__init__.py
    from apps.prioritization.consumers.priority_recalculator import PriorityRecalculator

The class is discovered by importing ``apps/<context>/consumers/__init__.py`` at app-ready time,
which is why the class must be re-exported from that ``__init__``. It then runs under
``manage.py run_consumer <group>``.

**What ``handle`` may assume.**

* It is called inside an open ``transaction.atomic()`` that already contains the ``ProcessedEvent``
  claim for ``(envelope.id, group)``. Everything it writes commits with that claim or not at all,
  which is what makes a retry a real retry instead of a silent skip.
* It is never called twice for the same ``envelope.id`` in the same group *after a success*. It
  can absolutely be called twice after a failure, and will be called for an envelope another group
  already processed. Deduplication across groups is per group, by design.
* ``envelope.topic`` is always in ``self.topics``; the runner acknowledges and skips everything
  else without claiming it, so subscribing to more topics later replays nothing.
* Time comes from ``envelope.occurred_at``, not from the wall clock. A redelivery must land on the
  same result, so a handler that reads ``timezone.now()`` for domain logic is not idempotent.

**What ``handle`` must not do.**

* Do not catch its own failures to "keep the stream moving". Raising is how the retry, the log
  line and the dead letter happen; a swallowed exception is an event that vanished.
* Do not call ``transaction.commit`` / ``set_autocommit``, and do not open a second connection.
  Breaking the enclosing transaction breaks the deduplication guarantee.
* Do not ``XACK``, publish, or touch Redis. The runner owns the stream.

**Delivery semantics.** At-least-once. An unexpected exception is logged with ``topic``,
``event_id`` and ``consumer_group``, retried with exponential backoff and jitter up to
``settings.EVENT_MAX_ATTEMPTS``, and then written to ``settings.EVENT_DLQ_STREAM`` with the
original envelope, the error, the attempt count, the group and the original stream entry id. The
entry is acknowledged after the transaction commits — including on the duplicate path and after a
dead letter — because an unacked entry stays pending forever and quietly grows the group's lag.
The single exception is a dead letter write that itself fails: the entry is left pending so the
event is redelivered rather than lost.
"""

import logging
import random
import time
from abc import ABC, abstractmethod
from socket import gethostname
from typing import Any, ClassVar, Final
from uuid import UUID

from django.conf import settings
from django.db import IntegrityError, transaction
from pydantic import ValidationError
from redis import Redis
from redis.exceptions import RedisError, ResponseError

from apps.events.domain.envelope import ENVELOPE_FIELD, EventEnvelope
from apps.events.domain.errors import ConsumerGroupNotRegisteredError, EnvelopeDecodeError
from apps.events.models import ProcessedEvent
from apps.events.redis_client import create_redis_client

logger = logging.getLogger(__name__)

#: First backoff step. The sequence is roughly 1s, 2s, 4s, 8s, 16s (EVENTS.md §6).
RETRY_BASE_SECONDS: Final[float] = 1.0

#: Jitter as a fraction of the computed delay, so two consumers in a group that failed on the
#: same event do not retry in lockstep and hammer whatever was already unhealthy.
RETRY_JITTER_RATIO: Final[float] = 0.25

#: How long ``XREADGROUP`` blocks waiting for work. Long enough that an idle group costs nothing,
#: short enough that a shutdown signal is noticed promptly.
DEFAULT_BLOCK_MS: Final[int] = 5_000

#: Entries fetched per read. Small: each one is processed in its own transaction, and a large
#: batch only lengthens the window in which a crash re-delivers work.
DEFAULT_READ_COUNT: Final[int] = 10

_BUSY_GROUP_MARKER: Final[str] = "BUSYGROUP"
_ERROR_TEXT_LIMIT: Final[int] = 500
_jitter = random.SystemRandom()


class EventConsumer(ABC):
    """One reason to react to events, and the unit that a consumer group runs.

    One group per reason, never one per topic: a group that fails must not stop the others, and a
    new topic joins an existing group unless it needs to fail independently (PATTERNS_BACKEND §7).

    Attributes:
        group: The Redis consumer group name, and the ``consumer_group`` half of the idempotency
            key. It is in `docs/EVENTS.md` §5 or it is not deployed.
        topics: The subscription. Envelopes outside it are acknowledged and ignored without a
            ``ProcessedEvent`` row, so widening the set later does not replay history as
            duplicates — it simply starts claiming what it now cares about.
    """

    group: ClassVar[str]
    topics: ClassVar[frozenset[str]]

    @abstractmethod
    def handle(self, envelope: EventEnvelope) -> None:
        """Apply the event's effect, inside the runner's transaction.

        Raising is the documented way to fail: the runner logs, retries with backoff and finally
        dead letters. Returning normally means the effect is committed together with the claim.

        Args:
            envelope: The event, already validated and already filtered to ``self.topics``.

        Raises:
            Exception: Anything the effect can fail with. The runner treats every exception the
                same way — it does not interpret them — so a handler that wants different
                behaviour for a permanent failure must decide that itself before raising.
        """


_REGISTRY: dict[str, type[EventConsumer]] = {}


def register_consumer(consumer_class: type[EventConsumer]) -> type[EventConsumer]:
    """Register a consumer class under its declared ``group``.

    A registry rather than a settings list because the group name already lives on the class, and
    two places naming the same string is how a group ends up deployed under a typo.

    Args:
        consumer_class: A concrete :class:`EventConsumer` subclass with ``group`` and ``topics``.

    Returns:
        The same class, so this reads as a decorator.

    Raises:
        ValueError: The class does not declare ``group``/``topics``, or the group name is already
            taken by another class. A silently overwritten registration would deploy one of two
            consumers at random.
    """
    group = getattr(consumer_class, "group", "")
    topics = getattr(consumer_class, "topics", None)
    if not group or topics is None:
        message = f"{consumer_class.__name__} must declare both 'group' and 'topics'."
        raise ValueError(message)
    existing = _REGISTRY.get(group)
    if existing is not None and existing is not consumer_class:
        message = f"Consumer group {group!r} is already registered by {existing.__name__}."
        raise ValueError(message)
    _REGISTRY[group] = consumer_class
    return consumer_class


def registered_groups() -> tuple[str, ...]:
    """Every group name currently registered, sorted, for command help and error messages."""
    return tuple(sorted(_REGISTRY))


def get_consumer(group: str) -> EventConsumer:
    """Instantiate the consumer registered under a group name.

    Args:
        group: The group name, e.g. ``"priority-recalculator"``.

    Returns:
        A fresh consumer instance. Consumers are stateless between events, so the runner holds one
        for its whole lifetime.

    Raises:
        ConsumerGroupNotRegisteredError: Nothing registered that name — almost always a consumers
            package that is not re-exported from its ``__init__``, so app-ready never imported it.
    """
    consumer_class = _REGISTRY.get(group)
    if consumer_class is None:
        raise ConsumerGroupNotRegisteredError(group, registered_groups())
    return consumer_class()


def apply_once(consumer: EventConsumer, envelope: EventEnvelope) -> bool:
    """Run a handler exactly once per (event, group), claim and effect in one transaction.

    The ``ProcessedEvent`` insert and the handler's writes share a transaction, so the claim is
    only durable if the effect is. No ``SELECT`` precedes the insert: check-then-insert races
    between two consumers of the same group, whereas the unique constraint decides atomically.

    Args:
        consumer: The registered consumer whose ``group`` forms half of the idempotency key.
        envelope: The delivered event.

    Returns:
        True when the effect was applied by this call; False when the event had already been
        applied by this group and the call was a no-op. Both outcomes are successes and both must
        be acknowledged — an unacked duplicate stays pending forever.

    Raises:
        Exception: Whatever the handler raised, after the transaction has rolled back. The claim
            rolls back with it, which is what makes the next attempt a real retry.
    """
    with transaction.atomic():
        if not _claim(consumer.group, envelope.id):
            logger.debug(
                "duplicate event ignored",
                extra={
                    "event_id": str(envelope.id),
                    "topic": envelope.topic,
                    "consumer_group": consumer.group,
                },
            )
            return False
        consumer.handle(envelope)
    return True


def _claim(group: str, event_id: UUID) -> bool:
    """Insert the idempotency row inside a savepoint, so a duplicate does not poison the outer tx."""
    try:
        with transaction.atomic():
            ProcessedEvent.objects.create(event_id=event_id, consumer_group=group)
    except IntegrityError:
        return False
    else:
        return True


class ConsumerRunner:
    """Reads one consumer group off ``aztec.events`` and drives its consumer.

    Owns everything the handler is forbidden to touch: the group, the read loop, the retry budget,
    the dead letter stream and the acknowledgement. Several runners can share a group — that is
    what the group is for — as long as each has a distinct ``consumer_name``.
    """

    def __init__(
        self,
        consumer: EventConsumer,
        *,
        redis_client: Redis | None = None,
        stream: str | None = None,
        dlq_stream: str | None = None,
        max_attempts: int | None = None,
        consumer_name: str | None = None,
    ) -> None:
        self._consumer = consumer
        self._redis = redis_client if redis_client is not None else create_redis_client()
        self._stream = stream if stream is not None else settings.EVENT_STREAM
        self._dlq_stream = dlq_stream if dlq_stream is not None else settings.EVENT_DLQ_STREAM
        self._max_attempts = (
            max_attempts if max_attempts is not None else settings.EVENT_MAX_ATTEMPTS
        )
        self._consumer_name = (
            consumer_name if consumer_name is not None else f"{consumer.group}@{gethostname()}"
        )

    def ensure_group(self) -> None:
        """Create the consumer group if it does not exist, starting at the head of the stream.

        The group is created at id ``0`` rather than ``$`` so a group added after events already
        flowed catches up instead of starting blind. Replaying history is safe precisely because
        every effect is claimed through ``ProcessedEvent``.
        """
        try:
            self._redis.xgroup_create(
                name=self._stream, groupname=self._consumer.group, id="0", mkstream=True
            )
        except ResponseError as error:
            if _BUSY_GROUP_MARKER not in str(error):
                raise
            logger.debug("consumer group already exists", extra={"group": self._consumer.group})

    def run_once(self, *, block_ms: int = DEFAULT_BLOCK_MS, count: int = DEFAULT_READ_COUNT) -> int:
        """Read one batch of undelivered entries and process each of them.

        Args:
            block_ms: How long to block when the stream is idle.
            count: Maximum entries per read.

        Returns:
            The number of entries processed, so a caller can distinguish an idle group from a
            busy one without inspecting Redis.
        """
        return self._read_and_process(">", block_ms=block_ms, count=count)

    def recover_pending(self, *, count: int = DEFAULT_READ_COUNT) -> int:
        """Reprocess entries this consumer name claimed but never acknowledged.

        Called once at startup: a crash between the handler's commit and the ``XACK`` leaves the
        entry pending, and without this it would sit in the group's PEL until someone noticed the
        lag. Reprocessing is safe — the second attempt deduplicates and acknowledges.

        Returns:
            The number of pending entries reprocessed.
        """
        return self._read_and_process("0", block_ms=0, count=count)

    def run_forever(self) -> None:  # pragma: no cover - the body is exercised through run_once
        """Recover this consumer's pending entries, then read the group until the process stops."""
        self.ensure_group()
        recovered = self.recover_pending()
        logger.info(
            "consumer started",
            extra={
                "consumer_group": self._consumer.group,
                "consumer_name": self._consumer_name,
                "recovered_pending": recovered,
                "topics": sorted(self._consumer.topics),
            },
        )
        while True:
            self.run_once()

    def _read_and_process(self, start_id: str, *, block_ms: int, count: int) -> int:
        """Issue one XREADGROUP and process whatever it returns."""
        # redis-py types this response as a loosely-shaped nested structure; the decoded shape is
        # [(stream_name, [(entry_id, {field: value}), ...]), ...].
        response: Any = self._redis.xreadgroup(
            groupname=self._consumer.group,
            consumername=self._consumer_name,
            streams={self._stream: start_id},
            count=count,
            block=block_ms,
        )
        if not response:
            return 0
        processed = 0
        for _stream_name, entries in response:
            for entry_id, fields in entries:
                self._process_entry(str(entry_id), fields)
                processed += 1
        return processed

    def _process_entry(self, entry_id: str, fields: dict[str, str]) -> None:
        """Decode, filter, deliver and acknowledge a single stream entry."""
        raw = fields.get(ENVELOPE_FIELD, "")
        envelope = self._decode(entry_id, raw)
        if envelope is None:
            return
        if envelope.topic not in self._consumer.topics:
            self._ack(entry_id)
            return
        if self._deliver_with_retry(entry_id, envelope):
            self._ack(entry_id)

    def _decode(self, entry_id: str, raw: str) -> EventEnvelope | None:
        """Rebuild the envelope, or dead letter the entry when it is not one."""
        try:
            return EventEnvelope.from_json(raw)
        except ValidationError as error:
            decode_error = EnvelopeDecodeError(entry_id, str(error))
            logger.exception(
                "undecodable stream entry",
                extra={"entry_id": entry_id, "consumer_group": self._consumer.group},
            )
            if self._dead_letter(entry_id, raw, str(decode_error), attempts=0):
                self._ack(entry_id)
            return None

    def _deliver_with_retry(self, entry_id: str, envelope: EventEnvelope) -> bool:
        """Attempt the handler up to the retry budget, then dead letter.

        Returns:
            True when the entry may be acknowledged: applied, duplicate, or safely dead lettered.
            False only when the dead letter write itself failed, in which case the entry is left
            pending so the event is redelivered rather than lost.
        """
        last_error = ""
        for attempt in range(1, self._max_attempts + 1):
            try:
                apply_once(self._consumer, envelope)
            # The consumer boundary is the one place a broad catch is correct (BACKEND.md §5):
            # an escaping exception would kill the loop and stall the entire group. Nothing is
            # swallowed here — it is logged with the event id and the group, retried with
            # backoff, and finally routed to the dead letter stream.
            except Exception as error:
                last_error = f"{type(error).__name__}: {error}"[:_ERROR_TEXT_LIMIT]
                logger.exception(
                    "consumer handler failed",
                    extra={
                        "event_id": str(envelope.id),
                        "topic": envelope.topic,
                        "consumer_group": self._consumer.group,
                        "attempt": attempt,
                    },
                )
                if attempt < self._max_attempts:
                    time.sleep(_backoff_seconds(attempt))
            else:
                return True
        return self._dead_letter(
            entry_id, envelope.to_json(), last_error, attempts=self._max_attempts
        )

    def _dead_letter(self, entry_id: str, data: str, error: str, *, attempts: int) -> bool:
        """Write the failure to ``aztec.events.dlq`` so it is loud and replayable.

        The entry carries the original envelope plus what a human needs to decide what to do:
        the error, the attempt count, the group that failed and the stream entry id it failed on.
        Replay re-publishes the same envelope with the same ``event.id`` onto ``aztec.events``;
        groups that already applied it deduplicate, and the group that failed applies it once.

        Returns:
            True when the dead letter is durably written and the entry may be acknowledged.
        """
        try:
            self._redis.xadd(
                self._dlq_stream,
                {
                    ENVELOPE_FIELD: data,
                    "error": error,
                    "attempts": attempts,
                    "consumer_group": self._consumer.group,
                    "stream_entry_id": entry_id,
                    "stage": "consumer",
                },
            )
        except RedisError:
            logger.exception(
                "could not write dead letter; entry stays pending for redelivery",
                extra={"entry_id": entry_id, "consumer_group": self._consumer.group},
            )
            return False
        logger.error(
            "event dead lettered",
            extra={
                "entry_id": entry_id,
                "consumer_group": self._consumer.group,
                "attempts": attempts,
                "dlq_stream": self._dlq_stream,
            },
        )
        return True

    def _ack(self, entry_id: str) -> None:
        """Acknowledge after the transaction has committed. Ack-then-process loses work."""
        try:
            self._redis.xack(self._stream, self._consumer.group, entry_id)
        except RedisError:
            logger.exception(
                "could not acknowledge entry; it will be redelivered",
                extra={"entry_id": entry_id, "consumer_group": self._consumer.group},
            )


def _backoff_seconds(attempt: int) -> float:
    """Exponential delay with jitter: roughly 1s, 2s, 4s, 8s, 16s."""
    # The doubling is written as a float power on purpose: ``2 ** n`` with a runtime ``n`` is typed
    # as ``Any`` (a negative exponent would make it a float), which would let the delay leave this
    # function unchecked.
    delay: float = RETRY_BASE_SECONDS * 2.0 ** (attempt - 1)
    return delay + _jitter.uniform(0.0, delay * RETRY_JITTER_RATIO)
