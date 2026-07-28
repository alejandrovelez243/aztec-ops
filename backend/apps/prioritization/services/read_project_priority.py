"""Use case: read everything the prioritization context knows about one project.

The project detail endpoint needs the score with its argument, the override in force and the open
risk flags together — and it needs the *health* those flags imply. Assembling them here rather than
in the portfolio's read service is what stops health from being re-derived with a local ``if``:
:func:`~apps.prioritization.domain.specifications.derive_health` is the single definition, and this
service is the only place outside the risk evaluator that calls it.

Three queries, one per table, and none of them recomputes anything: what the API returns is what
the engine persisted, which is the whole basis of "explainable" (CLAUDE.md rule 7).
"""

from datetime import datetime

from pydantic import BaseModel, ConfigDict

from ..domain.specifications import derive_health
from ..domain.types import Health, RiskFlag, Severity
from ..domain.views import OverrideView, RiskFlagView, ScoreView
from ..models import PriorityOverride, PriorityScore
from ..models import RiskFlag as RiskFlagRow


class ProjectPriorityView(BaseModel):
    """The prioritization half of a project detail response.

    ``score`` is ``None`` only between a project's creation and the recalculator's first pass. It
    is not flattened to zero: "not scored yet" and "scored zero" rank identically and mean opposite
    things, and only the null tells a reader which one happened.
    """

    model_config = ConfigDict(frozen=True)

    score: ScoreView | None = None
    override: OverrideView | None = None
    risk_flags: tuple[RiskFlagView, ...] = ()
    health: Health = Health.HEALTHY


def read_project_priority(*, project_id: int, now: datetime) -> ProjectPriorityView:
    """Read the persisted score, the live override and the open flags of one project.

    Expiry is evaluated against ``now`` here rather than inside the named query, because a query
    must not read a clock its caller did not choose — a replay would otherwise resolve an old
    override against today.

    Args:
        project_id: Numeric primary key of the project. Numeric rather than the business code
            because every table in this context keys on it, and the caller resolving the project
            already holds the row.
        now: The instant an override's ``expires_at`` is compared against.

    Returns:
        The score, override, flags and the health those flags derive to. A project with no score,
        no override and no flags returns a healthy view rather than raising: nothing is wrong with
        a project the engine has found nothing to say about.
    """
    flag_rows = list(RiskFlagRow.objects.for_project(project_id).open().oldest_first())
    score = PriorityScore.objects.for_project(project_id).first()
    return ProjectPriorityView(
        score=score.to_view() if score is not None else None,
        override=_live_override(project_id=project_id, now=now),
        risk_flags=tuple(row.to_view() for row in flag_rows),
        health=derive_health(
            tuple(
                RiskFlag(code=row.code, severity=Severity(row.severity), detail=row.detail)
                for row in flag_rows
            )
        ),
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
