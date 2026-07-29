"""Use case: the bus's own vital signs, read off the outbox.

This is what replaced ``XPENDING`` and stream inspection when Celery became the transport. The
outbox is a PostgreSQL table, so "how far behind is delivery" is a query rather than a redis-cli
session, and the answer is the same table the admin already renders.

**Informational, never a gate.** ``GET /api/v1/health/pipeline`` always returns 200 and this
function never raises for a bad number: a backlog is a fact about the workers, not about the API,
and wiring it into a container healthcheck would restart the one process still able to serve reads
because a poisoned event is stuck. Readiness checks dependencies; this reports on work.

The numbers are a snapshot at one instant and are read without a transaction on purpose. Locking
the outbox to count it would put the health endpoint in the way of the drain it is measuring.
"""

from datetime import datetime

from django.db.models import Count, Min
from django.utils import timezone
from pydantic import BaseModel, ConfigDict, Field

from apps.events.models import OutboxEvent


class PipelineHealth(BaseModel):
    """What the event pipeline looks like right now, in the four numbers that diagnose it.

    Read them together, because each one alone is ambiguous:

    * ``unpublished`` rising while ``oldest_unpublished_age_seconds`` rises too means the drain is
      not running. Rising with a low age means a burst that is being worked through.
    * ``dead_lettered`` above zero means a handler exhausted its attempt budget and an operator has
      to look; the events are queryable in the admin and re-queuable from it, not lost.
    * ``last_tick_at`` older than the tick interval means Celery Beat is down, which is silent
      otherwise: nothing fails, scores simply stop ageing.

    ``None`` in either age field means "nothing to measure" — an empty backlog, or a database that
    has never seen a tick — and is deliberately not zero, because zero would read as "perfectly
    fresh" for exactly the state that is most suspicious on a long-running deployment.
    """

    model_config = ConfigDict(frozen=True)

    unpublished: int = Field(ge=0)
    oldest_unpublished_age_seconds: float | None = Field(default=None, ge=0)
    dead_lettered: int = Field(ge=0)
    last_tick_at: datetime | None = None
    last_tick_age_seconds: float | None = Field(default=None, ge=0)


def read_pipeline_health(*, now: datetime | None = None) -> PipelineHealth:
    """Measure the outbox backlog, the dead-letter count and the age of the last clock tick.

    Three queries and no joins, all of them served by the partial indexes the drain already needs,
    so polling this endpoint every few seconds costs the database nothing measurable.

    Args:
        now: The instant the ages are measured against. Defaults to the wall clock; passed in by a
            test so an age can be asserted exactly rather than approximately.

    Returns:
        The snapshot. Never raises on an empty table: a fresh database reports zero backlog and no
        tick, which is the honest reading and not an error.
    """
    at = now if now is not None else timezone.now()

    # One pass over the partial index for both numbers: counting and then re-scanning for the
    # oldest row would walk the same backlog twice for a single answer.
    backlog = OutboxEvent.objects.unpublished().aggregate(
        total=Count("id"), oldest=Min("occurred_at")
    )

    last_tick = OutboxEvent.objects.ticks().order_by("-occurred_at").first()
    last_tick_at = last_tick.occurred_at if last_tick is not None else None

    return PipelineHealth(
        unpublished=backlog["total"],
        oldest_unpublished_age_seconds=_age_seconds(backlog["oldest"], at),
        dead_lettered=OutboxEvent.objects.dead_lettered().count(),
        last_tick_at=last_tick_at,
        last_tick_age_seconds=_age_seconds(last_tick_at, at),
    )


def _age_seconds(moment: datetime | None, at: datetime) -> float | None:
    """How long ago ``moment`` was, or ``None`` when there is nothing to measure.

    Clamped at zero rather than allowed to go negative. A row whose ``occurred_at`` is slightly in
    the future is a clock skew between two processes, not a negative age, and a negative number
    here would only make a dashboard render nonsense.
    """
    if moment is None:
        return None
    return max(0.0, (at - moment).total_seconds())
