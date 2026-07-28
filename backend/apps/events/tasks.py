"""Scheduled producers.

The only tasks in the system. Both write ``clock.ticked`` to the outbox and return; they never
touch Redis Streams directly, so the scheduler cannot become a second, undocumented way onto the
bus. Celery Beat decides *when*; ``apps.events.ticker`` decides *what*.

Why the day boundary is a separate task rather than a branch inside the interval task: a crontab
entry states the intent directly, and the alternative — comparing the current local date against a
remembered one — needs process state that is wrong after every restart and after every scale-out
to a second worker.
"""

import logging

from celery import shared_task
from django.utils import timezone

from apps.events.ticker import TICK_KIND_DAY_BOUNDARY, TICK_KIND_INTERVAL, Ticker

logger = logging.getLogger(__name__)


@shared_task(name="events.emit_interval_tick")
def emit_interval_tick() -> str:
    """Emit the periodic tick that bounds how stale a time-derived score can be.

    Returns:
        The envelope id, as a string, so it appears in the Celery result and in Flower.
    """
    event_id = Ticker().emit(kind=TICK_KIND_INTERVAL, tick_at=timezone.now())
    return str(event_id)


@shared_task(name="events.emit_day_boundary_tick")
def emit_day_boundary_tick() -> str:
    """Emit the tick that makes calendar-derived flags re-evaluate when the local date changes.

    Overdue and days-open change at midnight, not on a five-minute grid, so this runs on its own
    schedule rather than waiting for the next interval tick to notice.

    Returns:
        The envelope id, as a string.
    """
    event_id = Ticker().emit(kind=TICK_KIND_DAY_BOUNDARY, tick_at=timezone.now())
    return str(event_id)
