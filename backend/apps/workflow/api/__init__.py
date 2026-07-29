"""HTTP surface of the workflow context: reading every state graph, and authoring them.

Reading a graph is any member's business — a board cannot draw a column nobody told it about.
Writing one is an ops lead's, on every route, because the shape of a lifecycle decides how everyone
else's work is allowed to behave. The admin stays as a second door, not as the only one: a lifecycle
reshapeable only by somebody holding a Django admin account is configurable by engineering, not by
the operation (ARCHITECTURE decision 6).

There is still no route that lists states on their own, and the legal moves out of the state an
aggregate is actually in are served with that aggregate — as ``transitions`` on the project
detail — never here. Authoring a graph and obeying one are separate concerns and stay that way.
"""

from apps.workflow.api.routers import router

__all__ = ["router"]
