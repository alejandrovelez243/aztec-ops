"""Persistence for the bus: the transactional outbox and the handler deduplication ledger.

Both tables live in one app because they are one concern (DATA_MODEL §7): getting a committed
fact out of PostgreSQL and applying it exactly once per handler. Fields, constraints, indexes,
named queries on the manager and the row-level state transitions only — dispatch lives in
``apps.events.tasks`` and the write port in ``apps.events.services``.

Since Celery became the bus, the transport under this table is a Celery task rather than a Redis
stream. Nothing about the durability guarantee moved: the row still commits with the state change,
and it is still the only place an undelivered event can be found.
"""

import uuid
from datetime import datetime

from django.db import models
from django.utils import timezone

from apps.events.domain.envelope import TOPIC_CLOCK_TICKED, EntityRef, EventEnvelope

#: ``last_error`` is a display column, not a log. Longer causes are truncated to keep the admin
#: list readable and the row narrow.
ERROR_COLUMN_LIMIT = 500


class OutboxEventQuerySet(models.QuerySet["OutboxEvent"]):
    """Named queries for the outbox. Every caller composes these instead of writing a filter."""

    def unpublished(self) -> "OutboxEventQuerySet":
        """Rows the drain has not dispatched yet — the backlog, and the health signal."""
        return self.filter(published_at__isnull=True)

    def dead_lettered(self) -> "OutboxEventQuerySet":
        """Rows a handler could not apply within the attempt budget."""
        return self.filter(dead_lettered_at__isnull=False)

    def ticks(self) -> "OutboxEventQuerySet":
        """Rows the clock wrote, newest last by the model's own ordering.

        The only evidence that Celery Beat is alive that does not require asking Celery: Beat's
        one job here is to fire the tick tasks, and a tick task's one effect is a row on this
        table. An empty result on a database that has been up for longer than the tick interval
        means the scheduler is not running, and the health endpoint says so without opening an
        inspection socket to the broker.
        """
        return self.filter(topic=TOPIC_CLOCK_TICKED)

    def claim_batch(self, limit: int) -> "OutboxEventQuerySet":
        """Lock the oldest unpublished rows for this transaction, skipping locked ones.

        ``skip_locked`` is what lets two workers drain the same backlog with no coordination:
        each takes a disjoint prefix of the ordered set instead of blocking on the other. The
        caller must already be inside ``transaction.atomic()`` — ``select_for_update`` outside one
        raises, which is the failure mode this docstring exists to name.

        Args:
            limit: Maximum rows to claim. Bounds how long the rows stay locked.

        Returns:
            The claimed rows, oldest first, matching the partial index in DATA_MODEL §10.3.
        """
        claimable = self.select_for_update(skip_locked=True).unpublished()
        return claimable.order_by("occurred_at", "id")[:limit]


class OutboxEvent(models.Model):
    """One event, written in the same transaction as the change it describes.

    Nothing publishes from a request path. The service writes this row inside its own
    ``transaction.atomic()``, so the event is exactly as durable as the mutation: a rollback takes
    the event with it and a commit cannot lose it. ``apps.events.tasks.drain_outbox`` then claims
    unpublished rows and dispatches one ``handle_event`` task per subscribed handler, which turns
    delivery into a retryable background problem rather than a dual write that fails silently
    (PATTERNS_BACKEND §1).

    Three operational facts are readable straight off this table, and that is what replaced stream
    inspection: rows accumulating with ``published_at IS NULL`` mean the drain is not running; a
    non-null ``dead_lettered_at`` with ``last_error`` means a handler exhausted its budget and the
    event needs a human; zero unpublished rows with a stale UI means a service never wrote the
    outbox at all, which is a service bug and not a transport bug.
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
    dead_lettered_at = models.DateTimeField(null=True, blank=True, default=None)
    attempts = models.SmallIntegerField(default=0)
    last_error = models.CharField(max_length=ERROR_COLUMN_LIMIT, blank=True, default="")

    objects = OutboxEventQuerySet.as_manager()

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
        ]
        indexes = [
            # The drain's claim query, and partial on purpose: the unpublished set is the working
            # set and is nearly always tiny, while the published set is the whole history and is
            # never scanned by it. Rows leave this index as they are published, so its size tracks
            # the backlog instead of the table (DATA_MODEL §10.3). The column order matches the
            # ORDER BY so SKIP LOCKED hands each worker a disjoint prefix.
            models.Index(
                fields=["occurred_at", "id"],
                condition=models.Q(published_at__isnull=True),
                name="events_outbox_unpublished_idx",
            ),
            # The "what is stuck" query the admin's dead-letter filter runs. Partial for the same
            # reason: poisoned events are rare and the index should stay the size of the problem.
            models.Index(
                fields=["-dead_lettered_at"],
                condition=models.Q(dead_lettered_at__isnull=False),
                name="events_outbox_deadletter_idx",
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

        Read by ``drain_outbox`` when it dispatches and by ``handle_event`` when it delivers, so
        what a handler sees stays byte-comparable with what the service intended. No timestamp is
        refreshed and no id is generated here: ``id`` was assigned when the row was written, before
        the producing transaction committed, which is what makes it a usable deduplication key.

        The delivery bookkeeping columns are deliberately absent from the projection:
        ``created_at``, ``published_at``, ``dead_lettered_at``, ``attempts`` and ``last_error``
        describe *this system's* delivery attempts, not the fact that happened, and a handler that
        could read them would start branching on how many times it was retried.

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

    def mark_published(self, published_at: datetime | None = None) -> None:
        """Record that every subscribed handler has been dispatched for this row.

        Published means *handed to the transport*, not *applied*: the handler tasks may still be
        queued, retrying or about to dead-letter. It is written by the drain inside the same
        transaction that claimed the row, so a crash mid-batch re-dispatches the prefix rather than
        skipping it, which the ``ProcessedEvent`` claim absorbs.

        Args:
            published_at: The instant to record. Defaults to now; passed in by the drain so a whole
                batch carries one timestamp.
        """
        self.published_at = published_at if published_at is not None else timezone.now()
        self.save(update_fields=["published_at"])

    def record_failure(self, error: BaseException, *, attempt: int) -> None:
        """Count one failed delivery and keep the cause where the admin renders it.

        Args:
            error: What the handler raised. Only its type and message are kept — a traceback
                belongs in the log, not in a list column.
            attempt: 1-based attempt number, taken from the Celery retry count so it counts
                attempts of *this* delivery rather than the lifetime of the row.
        """
        self.attempts = attempt
        self.last_error = f"{type(error).__name__}: {error}"[:ERROR_COLUMN_LIMIT]
        self.save(update_fields=["attempts", "last_error"])

    def mark_dead_lettered(self, error: BaseException, *, attempt: int) -> None:
        """Retire a poisoned event to the admin instead of losing it.

        This is the replacement for a dead letter stream, and it is deliberately a column rather
        than a second queue: the payload, the topic, the attempt count and the failure all stay on
        one queryable row that the admin already renders, and replay is :meth:`requeue`.

        Args:
            error: The failure that exhausted the budget.
            attempt: The attempt number that exhausted it.
        """
        self.attempts = attempt
        self.last_error = f"{type(error).__name__}: {error}"[:ERROR_COLUMN_LIMIT]
        self.dead_lettered_at = timezone.now()
        self.save(update_fields=["attempts", "last_error", "dead_lettered_at"])

    def requeue(self) -> None:
        """Put a dead-lettered event back on the drain's claim index.

        Safe to call on an event whose other handlers already succeeded: those handlers hold a
        ``ProcessedEvent`` row, so the redelivery is a no-op for them and a real retry only for the
        handler that failed. That is replay, and it is why the outbox is a better dead letter than
        a second stream.
        """
        self.published_at = None
        self.dead_lettered_at = None
        self.attempts = 0
        self.last_error = ""
        self.save(update_fields=["published_at", "dead_lettered_at", "attempts", "last_error"])


class ProcessedEvent(models.Model):
    """The idempotency ledger: one row per ``(event, handler)`` actually applied.

    The unique constraint below *is* the deduplication mechanism, not a safety net over one. A
    handler does not ``SELECT`` before inserting, because check-then-insert races between two
    workers running the same task; the insert either succeeds or raises ``IntegrityError``,
    atomically, inside the same transaction as the handler's own writes. A failed attempt
    therefore leaves no row, so a retry is a real retry and not a silent skip.

    ``handler`` is the registered handler name from ``apps.events.registry`` — one row per reactor,
    so two handlers on the same topic each apply the event exactly once and neither can mask the
    other's failure.

    ``event_id`` is intentionally not a foreign key to :class:`OutboxEvent`: this table is pruned
    on its own retention schedule and must stay independently deletable (DATA_MODEL §8).
    """

    event_id = models.UUIDField()
    handler = models.CharField(max_length=48)
    processed_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "events_processedevent"
        verbose_name = "processed event"
        verbose_name_plural = "processed events"
        ordering = ["-processed_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["event_id", "handler"],
                name="events_processedevent_unique_per_handler",
            ),
        ]
        indexes = [
            # The retention sweep key. Without a sweep this table outgrows every other one;
            # without this index the sweep is a sequential scan (DATA_MODEL §10.4).
            models.Index(fields=["processed_at"], name="events_processed_at_idx"),
        ]

    def __str__(self) -> str:
        """Identify the claim by the pair that makes it unique."""
        return f"{self.event_id} / {self.handler}"
