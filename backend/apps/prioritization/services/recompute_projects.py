"""Use case: rebuild score and risk flags for a set of projects, at one instant.

The manual override of an automatic system. In normal operation scores are maintained by the
``priority-recalculator`` and ``risk-evaluator`` handlers — a data change arrives as an event, and
a clock tick covers the projects whose ``valid_until`` has passed. There are exactly three moments
when nothing will arrive and rows have to be rebuilt anyway: right after seeding, right after a
new ``PriorityPolicy`` version is activated (every stored number was computed against weights that
are no longer the criterion), and when an operator has a reason to distrust a single row.

This used to be ``manage.py recompute``. A command someone runs from a laptop against the
production database is not an operation, it is an accident with a shell prompt, so the same
function is now reached from the Django admin action on ``Project`` and from the API endpoint.
Both call *this*; there is no second implementation to drift.

Ends at the database. It writes no ``ActivityRecord`` and no ``OutboxEvent``, exactly as the
command did not: recomputation is derivation, not a decision, and pushing twenty-two
``project.priority.recalculated`` events at a browser that is not open yet is noise. The scores
this writes are the same ones the handlers would have written, so nothing downstream is left
inconsistent — only uninformed until its next legitimate event.
"""

from collections.abc import Sequence
from datetime import datetime

from django.utils import timezone
from pydantic import BaseModel, ConfigDict

from apps.portfolio.models import Project

from ..domain.types import Health
from .evaluate_risk_for_project import evaluate_risk_for_project
from .recompute_for_project import recompute_for_project


class ProjectRecompute(BaseModel):
    """What one project's rebuild produced, in the terms an operator reads back.

    ``changed`` is the disjunction of the score's and the risk evaluation's own ``changed``: an
    operator who reruns a recomputation needs to know whether anything actually moved, and a run
    that reports every project unchanged is the expected outcome of a healthy system, not a
    failure.
    """

    model_config = ConfigDict(frozen=True)

    project_code: str
    value: float
    health: Health
    flags: tuple[str, ...]
    changed: bool


class RecomputeRun(BaseModel):
    """The whole run: what was scored, when, and how much of it moved.

    ``ran_at`` is the single instant every project in ``results`` was scored for — reported rather
    than inferred, because it is the answer to "against what clock was this ranking true".
    """

    model_config = ConfigDict(frozen=True)

    ran_at: datetime
    results: tuple[ProjectRecompute, ...]

    @property
    def changed_count(self) -> int:
        """How many projects moved. Zero on a repeat run of an already-current portfolio."""
        return sum(1 for result in self.results if result.changed)


def recompute_projects(
    *, project_codes: Sequence[str], now: datetime | None = None
) -> RecomputeRun:
    """Rebuild ``PriorityScore`` and ``RiskFlag`` for the named projects against the active policy.

    One ``now`` is shared by every project rather than read per project, so two projects with the
    same facts get the same number and the ranking cannot be perturbed by how long the loop took.
    Idempotent: on a second run the input hash and the policy version match, and every project
    reports unchanged — which is what makes ``seed`` safe to repeat.

    Not atomic across projects, and deliberately: each project is scored in its own transaction
    (both callees are ``@transaction.atomic``), so a portfolio-wide rebuild that fails on project
    eighteen leaves the first seventeen correct instead of rolling back work that was right.

    Args:
        project_codes: Business codes to rebuild, e.g. ``("PRJ-01", "PRJ-07")``. Order is
            preserved, so two runs over the same selection produce comparable logs. An empty
            sequence is a valid no-op run.
        now: The instant to score for. Defaults to the current time; supplied explicitly by tests
            and by any caller that needs a reproducible ranking.

    Returns:
        One entry per code, in the order given, plus the instant they share.

    Raises:
        ProjectNotFound: A code names no project. Raised on the first offending code, after the
            earlier ones have already been committed.
        ActivePolicyNotFound: No policy is active; the engine refuses to rank against an implicit
            criterion.
        PolicySignalMismatch: The active policy and the signal registry disagree.
        PolicyWeightsNotNormalized: The active policy's weights do not sum to 1.0.
    """
    ran_at = now if now is not None else timezone.now()

    results: list[ProjectRecompute] = []
    for code in project_codes:
        # Two calls because two writers: scoring persists the score, risk evaluation persists the
        # flags, and each table has exactly one writer. This runs them in the order the bus would;
        # the score's breakdown evaluates the specifications itself, so neither depends on the
        # other's rows.
        score = recompute_for_project(project_code=code, now=ran_at)
        risk = evaluate_risk_for_project(project_code=code, now=ran_at)
        results.append(
            ProjectRecompute(
                project_code=code,
                value=float(score.value),
                health=risk.health,
                flags=tuple(flag.code for flag in risk.flags),
                changed=score.changed or risk.changed,
            )
        )

    return RecomputeRun(ran_at=ran_at, results=tuple(results))


def recompute_active_portfolio(*, now: datetime | None = None) -> RecomputeRun:
    """Rebuild every active project, in ``Project.Meta.ordering`` order.

    A separate function rather than a nullable argument on :func:`recompute_projects`, because
    "all of them" is a different intent from "these three" and a parameter that silently means
    *everything* when omitted is the kind of default that empties a table (CLAUDE.md rule 13).
    Archived projects are excluded: they are not ranked, so scoring them writes rows nothing reads.

    Args:
        now: The instant to score for. Defaults to the current time.

    Returns:
        One entry per active project, plus the instant they share.

    Raises:
        ActivePolicyNotFound: No policy is active.
    """
    codes: list[str] = list(Project.objects.active().values_list("code", flat=True))
    return recompute_projects(project_codes=codes, now=now)
