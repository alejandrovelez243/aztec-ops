"""Prioritization use cases, one module and one public function each.

``recompute_for_project`` is what the ``priority-recalculator`` handler calls, and what
``recompute_projects`` loops over for the manual override reached from the admin action and the
API endpoint; ``evaluate_risk_for_project`` is what the ``risk-evaluator`` consumer calls and the only
writer of ``prioritization_riskflag``; ``apply_override`` is what the API's override endpoint
calls. None of them publishes to Redis and none writes an ``ActivityRecord`` — the caller owns the
audit trail and the outbox row, in its own transaction, so a scoring change cannot roll back a
legitimate state change.

The split between the first two is not cosmetic. One table, one writer: two handlers
reconciling the same flags would let the second see the first's change already applied and stay
silent, and ``project.risk.changed`` would go missing precisely when the risk moved.
"""

from .apply_override import ApplyOverrideCommand, OverrideResult, apply_override
from .collect_project_facts import ProjectFacts, collect_project_facts
from .evaluate_risk_for_project import RiskEvaluationResult, evaluate_risk_for_project
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
    "ProjectRecompute",
    "RecomputeResult",
    "RecomputeRun",
    "RiskEvaluationResult",
    "apply_override",
    "collect_project_facts",
    "evaluate_risk_for_project",
    "recompute_active_portfolio",
    "recompute_for_project",
    "recompute_projects",
]
