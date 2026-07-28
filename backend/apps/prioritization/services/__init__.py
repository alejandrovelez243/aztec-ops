"""Prioritization use cases, one module and one public function each.

``recompute_for_project`` is what the ``priority-recalculator`` consumer and ``make recompute``
call; ``apply_override`` is what the API's override endpoint calls. Neither publishes to Redis and
neither writes an ``ActivityRecord`` — the caller owns the audit trail and the outbox row, in its
own transaction, so a scoring change cannot roll back a legitimate state change.
"""

from .apply_override import ApplyOverrideCommand, OverrideResult, apply_override
from .collect_project_facts import ProjectFacts, collect_project_facts
from .recompute_for_project import RecomputeResult, recompute_for_project

__all__ = [
    "ApplyOverrideCommand",
    "OverrideResult",
    "ProjectFacts",
    "RecomputeResult",
    "apply_override",
    "collect_project_facts",
    "recompute_for_project",
]
