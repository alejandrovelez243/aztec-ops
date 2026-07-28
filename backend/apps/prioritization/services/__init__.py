"""Prioritization use cases, one module and one public function each.

``recompute_for_project`` is what the ``priority-recalculator`` handler calls, and what
``recompute_projects`` loops over for the manual override reached from the admin action and the
API endpoint; ``apply_override`` is what the API's override endpoint calls;
``read_project_priority`` is what a read surface calls to get a score, the override in force and
the flags that hold *right now*. None of them publishes to Redis and none writes an
``ActivityRecord`` — the caller owns the audit trail and the outbox row, in its own transaction, so
a scoring change cannot roll back a legitimate state change.

There is no ``evaluate_risk_for_project``, and its absence is the design (ADR 0011). Flags are
derived from rows that already exist, so they are evaluated wherever they are read instead of being
reconciled into a table by a handler. A recomputation still evaluates them — the breakdown
denormalizes the raised codes — and carries them out in its result rather than persisting them.
"""

from .apply_override import ApplyOverrideCommand, OverrideResult, apply_override
from .collect_project_facts import ProjectFacts, collect_project_facts
from .read_project_priority import ProjectPriorityView, read_project_priority
from .recompute_for_project import RecomputeResult, recompute_for_project
from .recompute_projects import (
    ProjectRecompute,
    RecomputeRun,
    recompute_active_portfolio,
    recompute_projects,
)

__all__ = [
    "ApplyOverrideCommand",
    "OverrideResult",
    "ProjectFacts",
    "ProjectPriorityView",
    "ProjectRecompute",
    "RecomputeResult",
    "RecomputeRun",
    "apply_override",
    "collect_project_facts",
    "read_project_priority",
    "recompute_active_portfolio",
    "recompute_for_project",
    "recompute_projects",
]
