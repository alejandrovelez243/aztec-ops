"""HTTP surface of the ``work`` context: tasks, blockers and notes.

Project-scoped paths (``/projects/{code}/tasks``) live here rather than in the portfolio router,
because the use case behind them belongs to this context. The URL says where a task hangs; the
module says who owns it.

``api/`` never imports ``models``: every route calls a service. The services return the persisted
aggregate, and the route asks it to describe itself — ``task.to_view(today=…)`` — which is a
projection the model owns (CLAUDE.md rule 6), not logic the route performs.
"""

from apps.work.api.routers import router

__all__ = ["router"]
