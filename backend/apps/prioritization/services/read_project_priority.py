"""Use case: read everything the prioritization context knows about one project, right now.

The project detail endpoint needs the score with its argument, the override in force, the risk
flags that hold and the *health* those flags imply. Two of those four are stored and two are not,
and the split is the whole design (ADR 0011):

* the **score** is read from ``PriorityScore``, because a stored previous value is what makes
  ``PRIORITY_CHANGED`` expressible — a before, an after, and therefore an event;
* the **flags** are evaluated here, from :func:`~apps.prioritization.domain.specifications
  .evaluate_risk` over the facts as they are at ``now``. Overdue is ``due_date < today``, stale is
  ``last_activity < now - N days``, and the other four are pure functions of the current rows.
  Storing them would buy nothing and cost invalidation: a stored derivation can be wrong, a
  computed one cannot.

Health is derived from the flags by the one function that derives it, so it can never disagree with
the flags printed beside it.
"""

from datetime import datetime

from pydantic import BaseModel, ConfigDict

from ..domain.specifications import derive_health, evaluate_risk
from ..domain.types import Health
from ..domain.views import OverrideView, RiskFlagView, ScoreView
from ..models import PriorityOverride, PriorityScore
from .collect_project_facts import collect_project_facts


class ProjectPriorityView(BaseModel):
    """The prioritization half of a project detail response.

    ``score`` is ``None`` only between a project's creation and the recalculator's first pass. It
    is not flattened to zero: "not scored yet" and "scored zero" rank identically and mean opposite
    things, and only the null tells a reader which one happened.

    ``risk_flags`` is never ``None`` and never stale: it was computed from the rows this read
    already resolved, at the ``now`` the caller chose.
    """

    model_config = ConfigDict(frozen=True)

    score: ScoreView | None = None
    override: OverrideView | None = None
    risk_flags: tuple[RiskFlagView, ...] = ()
    health: Health = Health.HEALTHY


def read_project_priority(*, project_code: str, now: datetime) -> ProjectPriorityView:
    """Read the persisted score and live override, and evaluate the flags, at one instant.

    Expiry is evaluated against ``now`` here rather than inside the named query, because a query
    must not read a clock its caller did not choose — a replay would otherwise resolve an old
    override against today. The specifications are handed the same ``now`` for the same reason: a
    detail page whose score and whose "overdue" disagreed about the date is one nobody can reason
    about.

    Args:
        project_code: Business code, e.g. ``"PRJ-01"``. The code rather than the primary key,
            because the facts this evaluates are collected by code from the contexts that own them.
        now: The instant the flags are evaluated at and an override's ``expires_at`` compared
            against.

    Returns:
        The score, the override, the flags that hold at ``now`` and the health they derive to. A
        project with no score and no flags returns a healthy view rather than raising: nothing is
        wrong with a project the engine has found nothing to say about.

    Raises:
        ProjectNotFound: No project carries that code.
    """
    facts = collect_project_facts(project_code=project_code, now=now)
    flags = evaluate_risk(facts.signal_input.as_risk_input())
    score = PriorityScore.objects.for_project(facts.project_id).first()
    return ProjectPriorityView(
        score=score.to_view() if score is not None else None,
        override=_live_override(project_id=facts.project_id, now=now),
        risk_flags=tuple(flag.to_view() for flag in flags),
        health=derive_health(flags),
    )


def _live_override(*, project_id: int, now: datetime) -> OverrideView | None:
    """The override forcing this project's position at ``now``, or ``None``.

    An expired override is reported as absent rather than as present-and-stale: it no longer moves
    the project, so rendering it would label a row as manually forced when the ranking it shows is
    the engine's own.
    """
    override = PriorityOverride.objects.for_project(project_id).live().first()
    if override is None:
        return None
    if override.expires_at is not None and override.expires_at <= now:
        return None
    return override.to_view()
