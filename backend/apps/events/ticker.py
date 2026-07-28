"""The clock as an explicit participant on the bus.

Two prioritization signals — ``deadline_pressure`` and ``staleness`` — are functions of *now*
rather than of any mutation. A project crosses its target date, or goes stale, without anybody
touching it, and a purely change-driven system never notices. So the clock emits ``clock.ticked``
like any other producer: through the outbox, never straight to Redis (ARCHITECTURE §4.2).

Two kinds of tick. ``interval`` fires every ``settings.TICKER_INTERVAL_SECONDS`` and bounds how
stale a time-derived score can get. ``day_boundary`` fires when the local date rolls over, because
calendar-derived facts — overdue, days open — change at midnight and not on a five-minute grid.
Consumers select only the scores whose ``valid_until`` has passed, so a quiet tick costs one index
scan and emits nothing.

*When* either fires is Celery Beat's decision (``config.settings.CELERY_BEAT_SCHEDULE``), not this
module's. This class only knows how to write a tick. That split is deliberate: the earlier version
looped in-process and remembered the last local date it saw, which is state that is wrong after a
restart and duplicated the moment a second replica exists. A crontab entry needs neither.
"""

import logging
from datetime import UTC, datetime
from typing import Final
from uuid import UUID

from django.db import transaction

from apps.events.domain.envelope import (
    ENTITY_CLOCK,
    SYSTEM_ACTOR,
    TOPIC_CLOCK_TICKED,
)
from apps.events.services import enqueue_event

logger = logging.getLogger(__name__)

#: A tick on the fixed interval. Bounds how far a time-dependent score can drift.
TICK_KIND_INTERVAL: Final[str] = "interval"

#: The tick emitted once when the local date changes; consumers re-evaluate calendar-derived flags.
TICK_KIND_DAY_BOUNDARY: Final[str] = "day_boundary"

#: ``entity.id`` for the clock. There is no project in scope — the consumer decides which projects
#: a tick affects (EVENTS.md §4).
CLOCK_ENTITY_ID: Final[str] = "system"


class Ticker:
    """Writes ``clock.ticked`` to the outbox.

    Stateless by design. Scheduling lives in Celery Beat, so there is nothing to remember between
    ticks and nothing that goes wrong when the process restarts or a second replica starts.

    It writes outbox rows and nothing else. Whether a tick reaches a consumer is the relay's
    problem, which is why a Redis outage delays recomputation instead of losing the ticks.
    """

    def emit(self, *, kind: str, tick_at: datetime) -> UUID:
        """Write one tick to the outbox.

        Args:
            kind: ``interval`` or ``day_boundary``.
            tick_at: The instant the tick represents. Consumers use this and never their own wall
                clock, so a replayed tick recomputes deterministically.

        Returns:
            The envelope id, which is what a consumer deduplicates on.
        """
        with transaction.atomic():
            envelope = enqueue_event(
                topic=TOPIC_CLOCK_TICKED,
                entity_type=ENTITY_CLOCK,
                entity_id=CLOCK_ENTITY_ID,
                payload={"tick_at": _isoformat_utc(tick_at), "kind": kind},
                actor=SYSTEM_ACTOR,
                occurred_at=tick_at,
            )
        logger.info("clock ticked", extra={"kind": kind, "event_id": str(envelope.id)})
        return envelope.id


def _isoformat_utc(moment: datetime) -> str:
    """Render an aware instant the way every timestamp on the bus is rendered."""
    return moment.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
