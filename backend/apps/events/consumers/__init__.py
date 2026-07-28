"""Consumer-group runtime shared by every context that reacts to events.

Concrete consumers do not live here: they live in the context that owns the reaction —
``apps.prioritization.consumers`` (``priority-recalculator``, ``risk-evaluator``),
``apps.portfolio.consumers`` (``snapshot-rebuild``) and the SSE fanout. This package owns the
loop, the deduplication and the dead lettering, so those three never reimplement any of it.
"""

from apps.events.consumers.base import (
    ConsumerRunner,
    EventConsumer,
    apply_once,
    get_consumer,
    register_consumer,
    registered_groups,
)

__all__ = [
    "ConsumerRunner",
    "EventConsumer",
    "apply_once",
    "get_consumer",
    "register_consumer",
    "registered_groups",
]
