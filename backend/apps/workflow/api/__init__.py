"""HTTP surface of the workflow context: one read of the graph shapes, and nothing else.

There is no write route, and no route that lists states on their own. The graphs are operator data
edited in the admin (ARCHITECTURE decision 6), and the legal moves out of the state an aggregate is
actually in are served with that aggregate — as ``transitions`` on the project detail — never here.
"""

from apps.workflow.api.routers import router

__all__ = ["router"]
