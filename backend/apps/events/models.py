"""Persistence for the bus: the transactional outbox and the consumer deduplication ledger.

Both tables live in one app because they are one concern (DATA_MODEL §7): getting a committed
fact out of PostgreSQL and applying it exactly once per consumer group. Fields, constraints,
indexes and ``__str__`` only — the claim query lives in ``relay.py``, the write port in
``services.py`` and the deduplication in ``consumers/base.py``.
"""

import uuid

from django.db import models

from apps.events.domain.envelope import EntityRef, EventEnvelope


class OutboxEvent(models.Model):
    """One event, written in the same transaction as the change it describes.

    Nothing publishes to Redis from a request path. The service writes this row inside its own
    ``transaction.atomic()``, so the event is exactly as durable as the mutation: a rollback takes
    the event with it and a commit cannot lose it. The relay then claims unpublished rows and
    XADDs them, which turns delivery into a retryable background problem rather than a dual write
    that fails silently (PATTERNS_BACKEND §1).

    Two operational facts are readable straight off this table: rows accumulating with
    ``published_at IS NULL`` and a rising ``attempts`` mean the relay is down or Redis is
    unreachable; zero such rows with a stale UI means a service never wrote the outbox at all,
    which is a service bug and not a relay bug.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    topic = models.CharField(max_length=64)
    entity_type = models.CharField(max_length=16)
    entity_id = models.CharField(max_length=32)
    payload = models.JSONField(default=dict, blank=True)
    actor = models.CharField(max_length=32)
    correlation_id = models.UUIDField()
    version = models.PositiveSmallIntegerField(default=1)
    occurred_at = models.DateTimeField()
    created_at = models.DateTimeField(auto_now_add=True)
    published_at = models.DateTimeField(null=True, blank=True, default=None)
    stream_entry_id = models.CharField(max_length=32, blank=True, default="")
    attempts = models.SmallIntegerField(default=0)
    last_error = models.CharField(max_length=500, blank=True, default="")

    class Meta:
        db_table = "events_outboxevent"
        verbose_name = "outbox event"
        verbose_name_plural = "outbox events"
        ordering = ["-occurred_at", "-created_at"]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(attempts__gte=0),
                name="events_outboxevent_attempts_non_negative",
            ),
            models.CheckConstraint(
                condition=models.Q(published_at__isnull=True) | ~models.Q(stream_entry_id=""),
                name="events_outboxevent_published_has_entry_id",
            ),
        ]
        indexes = [
            # The relay's claim query, and partial on purpose: the unpublished set is the working
            # set and is nearly always tiny, while the published set is the whole history and is
            # never scanned by it. Rows leave this index as they are published, so its size tracks
            # the backlog instead of the table (DATA_MODEL §10.3). The column order matches the
            # ORDER BY so SKIP LOCKED hands each relay process a disjoint prefix.
            models.Index(
                fields=["occurred_at", "id"],
                condition=models.Q(published_at__isnull=True),
                name="events_outbox_unpublished_idx",
            ),
            models.Index(fields=["topic", "-occurred_at"], name="events_outbox_topic_idx"),
            models.Index(fields=["entity_type", "entity_id"], name="events_outbox_entity_idx"),
            models.Index(fields=["correlation_id"], name="events_outbox_correlation_idx"),
        ]

    def __str__(self) -> str:
        """Identify the event by what an operator searches for: topic and business code."""
        return f"{self.topic} {self.entity_type}:{self.entity_id} ({self.id})"

    def to_envelope(self) -> EventEnvelope:
        """Reassemble the envelope the producing service validated, deriving nothing.

        Read by :class:`apps.events.relay.OutboxRelay` — once to ``XADD`` the row onto
        ``settings.EVENT_STREAM``, and again to write it to the dead letter stream once the
        attempt budget is spent — so a published envelope stays byte-comparable with what the
        service intended. No timestamp is refreshed and no id is generated here: ``id`` was
        assigned when the row was written, before the producing transaction committed, which is
        what makes it a usable deduplication key for every consumer group.

        The delivery bookkeeping columns are deliberately absent from the projection:
        ``created_at``, ``published_at``, ``stream_entry_id``, ``attempts`` and ``last_error``
        describe *this relay's* attempts, not the fact that happened, and a consumer that could
        read them would start branching on how many times it was retried.

        Returns:
            The envelope as EVENTS.md §1 fixes it. Issues no query — every field is on this row.
        """
        return EventEnvelope(
            id=self.id,
            topic=self.topic,
            occurred_at=self.occurred_at,
            actor=self.actor,
            correlation_id=self.correlation_id,
            entity=EntityRef(type=self.entity_type, id=self.entity_id),
            payload=self.payload,
            version=self.version,
        )


class ProcessedEvent(models.Model):
    """The idempotency ledger: one row per (event, consumer group) actually applied.

    The unique constraint below *is* the deduplication mechanism, not a safety net over one. A
    handler does not ``SELECT`` before inserting, because check-then-insert races between two
    consumers in the same group; the insert either succeeds or raises ``IntegrityError``,
    atomically, inside the same transaction as the handler's own writes. A failed attempt
    therefore leaves no row, so a retry is a real retry and not a silent skip.

    ``event_id`` is intentionally not a foreign key to :class:`OutboxEvent`: this table is pruned
    on its own retention schedule and must stay independently deletable (DATA_MODEL §8).
    """

    event_id = models.UUIDField()
    consumer_group = models.CharField(max_length=48)
    processed_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "events_processedevent"
        verbose_name = "processed event"
        verbose_name_plural = "processed events"
        ordering = ["-processed_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["event_id", "consumer_group"],
                name="events_processedevent_unique_per_group",
            ),
        ]
        indexes = [
            # The retention sweep key. Without a sweep this table outgrows every other one;
            # without this index the sweep is a sequential scan (DATA_MODEL §10.4).
            models.Index(fields=["processed_at"], name="events_processed_at_idx"),
        ]

    def __str__(self) -> str:
        """Identify the claim by the pair that makes it unique."""
        return f"{self.event_id} / {self.consumer_group}"
