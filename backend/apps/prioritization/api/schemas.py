"""Request and response schemas of the prioritization endpoints: the override, and the rebuild."""

from datetime import datetime
from decimal import Decimal

from ninja import Field, Schema
from pydantic import BaseModel, ConfigDict

from apps.prioritization.domain.types import Health
from apps.prioritization.domain.views import OverrideView, ScoreView
from apps.prioritization.services import RecomputeRun


class OverrideIn(Schema):
    """Body of ``POST /api/v1/projects/{code}/priority-override``.

    ``reason`` is a constraint, not a convention. It is required here, re-checked by the service
    and enforced by a database check, because a forced position with no recorded justification is
    indistinguishable from a bug three weeks later — and the queue is exactly where nobody would
    think to look for one.

    Exactly one of ``position`` and ``boost`` must be present. That rule is validated by the
    service rather than by this schema so it has a single home: the admin action and any future
    importer construct the same command and must be refused for the same reason, with the same
    typed error.
    """

    position: int | None = Field(default=None, ge=1)
    boost: Decimal | None = None
    reason: str = Field(min_length=1, max_length=500)
    expires_at: datetime | None = None


class OverrideOut(BaseModel):
    """Response of the override endpoint: the computed score, and the decision beside it.

    Both are returned deliberately. ``score.value`` is **unchanged** by the override — the client
    labels the row as manually forced and still shows what the engine thought, so a reader can see
    the size of the disagreement rather than only its result.
    """

    model_config = ConfigDict(frozen=True)

    code: str
    score: ScoreView | None = None
    override: OverrideView | None = None


class ProjectRecomputeOut(BaseModel):
    """One project's line of a rebuild report.

    ``changed`` is the interesting field and the reason the report is returned at all: an operator
    who forces a recomputation wants to know whether the stored ranking was wrong. A rebuild where
    every row reports ``false`` is a healthy system confirming itself, not a wasted call.
    """

    model_config = ConfigDict(frozen=True)

    project_code: str
    value: float
    health: Health
    flags: tuple[str, ...]
    changed: bool


class RecomputeOut(BaseModel):
    """Response of the recompute endpoints.

    ``ran_at`` is the single instant every project in ``items`` was scored for. It is reported
    rather than left to the client's own clock because it is the answer to "against what time was
    this ranking true" — the two time-derived signals make that question load-bearing.

    ``changed`` is derived from ``items`` by :meth:`of` and is never assembled independently, so a
    body claiming three projects moved while listing none is unrepresentable.
    """

    model_config = ConfigDict(frozen=True)

    ran_at: datetime
    changed: int = Field(ge=0)
    items: tuple[ProjectRecomputeOut, ...]

    @classmethod
    def of(cls, run: RecomputeRun) -> "RecomputeOut":
        """Project a service run onto the wire.

        Args:
            run: What :func:`apps.prioritization.services.recompute_projects` reported.

        Returns:
            The response body, in the order the projects were scored.
        """
        return cls(
            ran_at=run.ran_at,
            changed=run.changed_count,
            items=tuple(
                ProjectRecomputeOut(
                    project_code=result.project_code,
                    value=result.value,
                    health=result.health,
                    flags=result.flags,
                    changed=result.changed,
                )
                for result in run.results
            ),
        )
