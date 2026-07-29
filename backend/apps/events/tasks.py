"""The transport: Celery tasks that move a committed outbox row to its handlers.

Four tasks, and they are the whole bus.

* ``events.drain_outbox`` claims unpublished rows and dispatches one ``events.handle_event`` per
  subscribed handler per event. It is kicked by ``transaction.on_commit`` in
  ``apps.events.services.enqueue_event`` so latency is a broker round trip, and swept by Celery
  Beat every ``settings.EVENT_DRAIN_INTERVAL_SECONDS`` so nothing is stranded when the kick fails.
  Both paths read the same table, so neither can lose an event and any double dispatch is absorbed
  by the ledger.
* ``events.handle_event`` applies one handler to one event, exactly once, and retries with backoff
  until ``settings.EVENT_MAX_ATTEMPTS``, after which the outbox row is dead-lettered — marked, not
  deleted, and not moved to a second queue.
* ``events.emit_interval_tick`` and ``events.emit_day_boundary_tick`` are the clock, and they are
  producers like any other: they write to the outbox and return.

Delivery is at-least-once by construction. The drain can die after dispatching and before
committing ``published_at``, in which case the handlers are dispatched twice; it can never
dispatch zero times, because the row is only claimed while the transaction holds its lock. Every
handler therefore deduplicates through ``ProcessedEvent`` (EVENTS.md §6).
"""

import logging
import random
from functools import partial
from typing import Any, Final
from uuid import UUID

from celery import shared_task
from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.events.domain.envelope import EventEnvelope
from apps.events.models import OutboxEvent, ProcessedEvent
from apps.events.registry import HandlerRegistration, get_handler, handlers_for
from apps.events.ticker import TICK_KIND_DAY_BOUNDARY, TICK_KIND_INTERVAL, Ticker

logger = logging.getLogger(__name__)

#: First backoff step. The sequence is roughly 1s, 2s, 4s, 8s, 16s (EVENTS.md §6).
RETRY_BASE_SECONDS: Final[float] = 1.0

#: Jitter as a fraction of the computed delay, so two handlers that failed on the same downstream
#: outage do not retry in lockstep and hammer whatever was already unhealthy.
RETRY_JITTER_RATIO: Final[float] = 0.25

#: Outcomes of ``events.handle_event``, returned rather than only logged so the Celery result says
#: which of the three happened without correlating log lines.
OUTCOME_APPLIED: Final[str] = "applied"
OUTCOME_DUPLICATE: Final[str] = "duplicate"
OUTCOME_MISSING: Final[str] = "missing"

_jitter = random.SystemRandom()


@shared_task(name="events.drain_outbox")
def drain_outbox(batch_size: int | None = None) -> str:
    """Claim a batch of unpublished events and dispatch their handlers.

    Safe to run concurrently: the claim is ``SELECT ... FOR UPDATE SKIP LOCKED``, so two workers
    take disjoint prefixes of the same ordered backlog with no coordination, no leader election
    and no lock table. Resumable for the same reason — all the state is in the table, so a killed
    drain loses nothing and the next one continues from the oldest unpublished row.

    The row is marked published inside the claiming transaction and the handler tasks are queued
    from ``transaction.on_commit``, so a rolled-back drain queues nothing and a committed one
    cannot have queued a task for a row it failed to mark.

    Args:
        batch_size: Rows claimed in this pass. Defaults to ``settings.EVENT_DRAIN_BATCH_SIZE``.
            Large enough that a burst drains in a few round trips, small enough that one worker
            never holds the whole backlog locked.

    Returns:
        A sentence naming what this pass did, because the return value is what the admin renders
        in django_celery_results. "Dispatched 12 event(s); 41 still pending" answers the operator's
        actual question; a bare integer makes them go and count the backlog themselves.
    """
    limit: int = batch_size if batch_size is not None else settings.EVENT_DRAIN_BATCH_SIZE
    dispatched = _claim_and_dispatch(limit)
    more = dispatched == limit
    if more:
        # A full batch means there is more backlog, so re-queue rather than hold one long
        # transaction — that is what keeps a burst draining at broker speed.
        drain_outbox.delay(batch_size=limit)
    if not dispatched:
        return "Nothing to dispatch; the outbox is drained."
    pending = OutboxEvent.objects.unpublished().count()
    tail = " Batch was full, so another drain was queued." if more else ""
    return f"Dispatched {dispatched} event(s); {pending} still pending.{tail}"


@shared_task(bind=True, name="events.handle_event", max_retries=None)
def handle_event(self: Any, handler_name: str, event_id: str) -> str:
    """Apply one registered handler to one event, exactly once.

    The ``ProcessedEvent`` insert and the handler's own writes share a transaction, so the claim is
    durable only if the effect is: a duplicate delivery is a no-op, and a failed attempt leaves no
    claim, which makes the next attempt a real retry rather than a silent skip.

    Retries are Celery's, with exponential backoff and jitter. Past
    ``settings.EVENT_MAX_ATTEMPTS`` the outbox row is dead-lettered — ``dead_lettered_at`` and
    ``last_error`` are set, the failure is logged at ERROR with the event id, the topic and the
    handler, and the exception is re-raised so the Celery result is a failure too. Nothing is
    swallowed and nothing is deleted: the event stays in the admin, where the re-queue action can
    replay it.

    ``max_retries`` is ``None`` on the decorator because the budget is
    ``settings.EVENT_MAX_ATTEMPTS``, read here at call time; Celery's own limit is fixed when the
    task is declared and could not follow a settings override.

    Args:
        self: The bound task, for ``self.request.retries`` and ``self.retry``.
        handler_name: A name registered through ``apps.events.registry.register_handler``.
        event_id: The outbox row's id, as a string, because a Celery argument must be JSON.

    Returns:
        ``applied`` when this call ran the handler, ``duplicate`` when the pair was already
        claimed, ``missing`` when the outbox row no longer exists.

    Raises:
        HandlerNotRegisteredError: The name is not registered. Loud on purpose — the event is still
            durable and re-queuable, but a deploy is wrong.
        Exception: Whatever the handler raised, once the attempt budget is spent.
    """
    row = OutboxEvent.objects.filter(id=event_id).first()
    if row is None:
        logger.error(
            "delivery task names an outbox row that does not exist",
            extra={"event_id": event_id, "handler": handler_name},
        )
        return f"{handler_name}: {OUTCOME_MISSING} — event {event_id} is gone."

    registration = get_handler(handler_name)
    envelope = row.to_envelope()
    attempt = int(self.request.retries) + 1
    try:
        applied = apply_once(registration, envelope)
    # The delivery boundary is the one place a broad catch is correct (BACKEND.md §5): every
    # handler failure is treated the same way — logged with the event id, the topic and the
    # handler, retried with backoff, and finally dead-lettered on the row. Nothing is swallowed;
    # both branches below end in a raise.
    except Exception as error:
        context = {
            "event_id": str(envelope.id),
            "topic": envelope.topic,
            "handler": handler_name,
            "attempt": attempt,
        }
        if attempt >= settings.EVENT_MAX_ATTEMPTS:
            row.mark_dead_lettered(error, attempt=attempt)
            logger.exception(
                "event dead lettered; it stays in the outbox for the admin to re-queue",
                extra=context,
            )
            raise
        row.record_failure(error, attempt=attempt)
        logger.warning("event handler failed; retrying", extra=context, exc_info=True)
        raise self.retry(exc=error, countdown=_backoff_seconds(attempt)) from error
    outcome = OUTCOME_APPLIED if applied else OUTCOME_DUPLICATE
    return f"{handler_name}: {outcome} for {envelope.topic} {envelope.entity.id}."


@shared_task(name="events.emit_interval_tick")
def emit_interval_tick() -> str:
    """Emit the periodic tick that bounds how stale a time-derived score can be.

    Returns:
        A sentence naming the tick, which is what the admin renders. It deliberately does NOT
        report how many scores were due: that would mean importing prioritization into events,
        and each task reports its own work — the recalculator's own result row says what it found.
    """
    now = timezone.now()
    event_id = Ticker().emit(kind=TICK_KIND_INTERVAL, tick_at=now)
    return f"Interval tick emitted as {event_id} for {now.isoformat(timespec='seconds')}."


@shared_task(name="events.emit_day_boundary_tick")
def emit_day_boundary_tick() -> str:
    """Emit the tick that makes calendar-derived flags re-evaluate when the local date changes.

    Overdue and days-open change at midnight, not on a five-minute grid, so this runs on its own
    schedule rather than waiting for the next interval tick to notice.

    Returns:
        A sentence naming the local date that just started, which is the fact the tick exists to
        announce.
    """
    now = timezone.now()
    event_id = Ticker().emit(kind=TICK_KIND_DAY_BOUNDARY, tick_at=now)
    local_date = timezone.localtime(now).date()
    return f"Day boundary tick {event_id}: calendar flags re-evaluated for {local_date}."


def apply_once(registration: HandlerRegistration, envelope: EventEnvelope) -> bool:
    """Run a handler exactly once per ``(event, handler)``, claim and effect in one transaction.

    No ``SELECT`` precedes the insert: check-then-insert races between two workers holding the same
    delivery, whereas the unique constraint decides atomically.

    Args:
        registration: The handler whose ``name`` forms half of the idempotency key.
        envelope: The delivered event.

    Returns:
        True when the effect was applied by this call; False when the pair was already claimed and
        the call was a no-op. Both outcomes are successes.

    Raises:
        Exception: Whatever the handler raised, after the transaction has rolled back. The claim
            rolls back with it, which is what makes the next attempt a real retry.
    """
    with transaction.atomic():
        if not _claim(registration.name, envelope.id):
            logger.debug(
                "duplicate event ignored",
                extra={
                    "event_id": str(envelope.id),
                    "topic": envelope.topic,
                    "handler": registration.name,
                },
            )
            return False
        registration.handle(envelope)
    return True


def _claim_and_dispatch(limit: int) -> int:
    """Claim one batch and queue its handler tasks, in a single transaction."""
    with transaction.atomic():
        rows = list(OutboxEvent.objects.claim_batch(limit))
        if not rows:
            return 0
        published_at = timezone.now()
        for row in rows:
            for registration in handlers_for(row.topic):
                transaction.on_commit(partial(_queue_delivery, registration.name, str(row.id)))
            row.published_at = published_at
        OutboxEvent.objects.bulk_update(rows, ["published_at"])
        logger.debug("outbox batch dispatched", extra={"events": len(rows)})
        return len(rows)


def _queue_delivery(handler_name: str, event_id: str) -> None:
    """Queue one delivery task. Called only after the claiming transaction committed."""
    handle_event.delay(handler_name=handler_name, event_id=event_id)


def _claim(handler_name: str, event_id: UUID) -> bool:
    """Insert the idempotency row in a savepoint, so a duplicate does not poison the outer tx."""
    try:
        with transaction.atomic():
            ProcessedEvent.objects.create(event_id=event_id, handler=handler_name)
    except IntegrityError:
        return False
    else:
        return True


def _backoff_seconds(attempt: int) -> float:
    """Exponential delay with jitter: roughly 1s, 2s, 4s, 8s, 16s."""
    # The doubling is written as a float power on purpose: ``2 ** n`` with a runtime ``n`` is typed
    # as ``Any`` (a negative exponent would make it a float), which would let the delay leave this
    # function unchecked.
    delay: float = RETRY_BASE_SECONDS * 2.0 ** (attempt - 1)
    return delay + _jitter.uniform(0.0, delay * RETRY_JITTER_RATIO)
