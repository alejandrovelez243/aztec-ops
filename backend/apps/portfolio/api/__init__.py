"""HTTP surface of the portfolio context: the queue, projects and team load.

``api/`` translates HTTP into a service call and a service result into a response, and does
nothing else (ARCHITECTURE §7). It **never imports ``models``**: every read goes through a read
service and every write through a use case, so an endpoint cannot grow a query and a query cannot
grow an endpoint. It also never catches a domain error — the mapping to a status code lives once,
in :mod:`config.errors`.
"""

from apps.portfolio.api.routers import router

__all__ = ["router"]
