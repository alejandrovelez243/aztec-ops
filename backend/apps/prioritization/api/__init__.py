"""HTTP surface of the prioritization context: the manual override, and nothing else.

There is no endpoint that writes a score. The ranking is computed by the engine from persisted
facts and is only ever *argued with* — by an override, stored beside the score and never inside it,
carrying the reason that justifies it. An HTTP route that set ``PriorityScore.value`` would make a
typed number indistinguishable from a computed one, which is the failure the whole design of
ARCHITECTURE §4.2 exists to prevent.
"""

from apps.prioritization.api.routers import router

__all__ = ["router"]
