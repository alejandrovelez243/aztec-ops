"""Use cases of the ``events`` context: the outbox write port, and the pipeline's own vital signs.

Two modules, and they face in opposite directions. :func:`enqueue_event` is what every other
context calls to publish a fact — the write port, and the reason no service anywhere imports Redis.
:func:`read_pipeline_health` is the read that answers "is the bus keeping up", so that
``GET /api/v1/health/pipeline`` can report a backlog without a router learning what an
``OutboxEvent`` is.

Neither of them opens a transaction of its own. ``enqueue_event`` runs inside the caller's, which
is the whole guarantee of the outbox pattern; the health read is a snapshot and would gain nothing
from one.
"""

from .enqueue_event import enqueue_event
from .read_pipeline_health import PipelineHealth, read_pipeline_health

__all__ = ["PipelineHealth", "enqueue_event", "read_pipeline_health"]
