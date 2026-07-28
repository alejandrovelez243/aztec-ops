"""Frozen value objects the engine takes and returns.

``SignalInput`` is deliberately wide: it carries every fact any of the six signals needs, so no
strategy ever reaches for a model, a repository or a clock. ``now`` is a field for the same
reason — a score computed from ``datetime.now()`` is neither reproducible nor testable, and a
replay of an old event would invent a new time.
"""

from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class Severity(StrEnum):
    """Severity of a risk flag, ordered from advisory to blocking.

    A closed, structural vocabulary — unlike a taxonomy, the operation does not add values to it,
    because ``health`` is derived from these four and nothing else.
    """

    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


class Health(StrEnum):
    """Derived project health.

    Never stored on ``Project`` and never edited: it is a function of the open risk flags, so it
    cannot disagree with them.
    """

    HEALTHY = "HEALTHY"
    AT_RISK = "AT_RISK"
    BLOCKED = "BLOCKED"


class ProjectRiskInput(BaseModel):
    """Everything the six risk specifications are allowed to look at.

    Narrower than :class:`SignalInput` on purpose: a specification that cannot see the business
    value cannot start ranking, and a specification that cannot see a repository cannot query.
    """

    model_config = ConfigDict(frozen=True)

    project_code: str
    now: datetime
    is_archived: bool = False
    state_category: str = ""
    target_date: date | None = None
    next_step: str = ""
    has_in_progress_task: bool = False
    overdue_task_count: int = Field(default=0, ge=0)
    blocked_task_count: int = Field(default=0, ge=0)
    open_blocker_count: int = Field(default=0, ge=0)
    oldest_blocker_age_days: int | None = None
    days_since_last_activity: int | None = None
    staleness_threshold_days: int = Field(default=14, gt=0)
    owner_code: str = ""
    owner_load_points: int = Field(default=0, ge=0)
    owner_capacity_points: int = Field(default=0, ge=0)


class SignalInput(BaseModel):
    """Every fact the scoring engine needs about one project at one instant.

    Assembled once per recomputation from the repositories of the other contexts and then passed
    unchanged to every strategy. Counts are non-negative by construction, ``next_step`` is the
    empty string rather than ``None`` (matching the column, so "no next step" is one predicate),
    and ``now`` is explicit so the same input always yields the same score.
    """

    model_config = ConfigDict(frozen=True)

    project_code: str
    now: datetime

    target_date: date | None = None
    is_archived: bool = False
    state_category: str = ""
    next_step: str = ""
    has_in_progress_task: bool = False

    open_task_count: int = Field(default=0, ge=0)
    overdue_task_count: int = Field(default=0, ge=0)
    urgent_open_task_count: int = Field(default=0, ge=0)
    blocked_task_count: int = Field(default=0, ge=0)

    open_blocker_count: int = Field(default=0, ge=0)
    oldest_blocker_age_days: int | None = None

    business_value: Decimal | None = None
    currency: str = "USD"
    portfolio_max_business_value: Decimal | None = None

    last_activity_at: datetime | None = None
    days_since_last_activity: int | None = None
    staleness_threshold_days: int = Field(default=14, gt=0)

    owner_code: str = ""
    owner_load_points: int = Field(default=0, ge=0)
    owner_capacity_points: int = Field(default=0, ge=0)

    engagement_type_code: str = ""
    engagement_type_weight: Decimal = Decimal("1.00")

    def as_risk_input(self) -> ProjectRiskInput:
        """Project the same facts onto the narrower input the specifications accept.

        One assembly feeds both halves of the engine, so a score and its flags can never be
        computed from two different readings of the portfolio.
        """
        return ProjectRiskInput(
            project_code=self.project_code,
            now=self.now,
            is_archived=self.is_archived,
            state_category=self.state_category,
            target_date=self.target_date,
            next_step=self.next_step,
            has_in_progress_task=self.has_in_progress_task,
            overdue_task_count=self.overdue_task_count,
            blocked_task_count=self.blocked_task_count,
            open_blocker_count=self.open_blocker_count,
            oldest_blocker_age_days=self.oldest_blocker_age_days,
            days_since_last_activity=self.days_since_last_activity,
            staleness_threshold_days=self.staleness_threshold_days,
            owner_code=self.owner_code,
            owner_load_points=self.owner_load_points,
            owner_capacity_points=self.owner_capacity_points,
        )


class SignalResult(BaseModel):
    """One signal's normalized reading plus the sentence that justifies it.

    ``score`` is clamped to ``[0.0, 1.0]`` at construction rather than by each strategy, so a
    strategy's arithmetic can saturate without every author remembering to bound it. ``reason``
    is mandatory and non-empty: it is what the UI shows to defend a rank, so a score without a
    reason is a bug, and validation is where that is cheapest to catch.
    """

    model_config = ConfigDict(frozen=True)

    score: float = Field(ge=0.0, le=1.0)
    reason: str = Field(min_length=1)

    @classmethod
    def clamped(cls, score: float, reason: str) -> "SignalResult":
        """Build a result from a possibly out-of-range score.

        Args:
            score: Raw normalized value; values outside ``[0, 1]`` are clamped, not rejected.
            reason: Human-readable English sentence naming the fact that produced the score.

        Returns:
            A frozen result whose score is inside the signal range.
        """
        return cls(score=min(1.0, max(0.0, score)), reason=reason)


class RiskFlag(BaseModel):
    """A satisfied risk specification, as a value.

    Persisted as a ``RiskFlag`` row by the caller. ``severity`` comes from the registry entry and
    never from the row, so re-classifying a risk is a code change reviewed once rather than a
    data edit that leaves old rows disagreeing with new ones.
    """

    model_config = ConfigDict(frozen=True)

    code: str
    severity: Severity
    detail: str = ""


class SignalContribution(BaseModel):
    """One line of the persisted breakdown.

    Holds the invariant ``contribution == round(raw * weight * 100, 2)``: the number and its
    justification are built together, so they cannot drift apart on the way to the database.
    """

    model_config = ConfigDict(frozen=True)

    code: str
    raw: float = Field(ge=0.0, le=1.0)
    weight: Decimal
    contribution: Decimal
    reason: str = Field(min_length=1)


class ModifierApplied(BaseModel):
    """A multiplicative adjustment applied to the weighted sum.

    Modifiers multiply and never add: an engagement type that matters more stretches the whole
    score rather than granting a fixed bonus that would swamp the signals at the bottom.
    """

    model_config = ConfigDict(frozen=True)

    code: str
    factor: Decimal
    reason: str = Field(min_length=1)


class ScoreBreakdown(BaseModel):
    """The full explanation of one score, serialized verbatim into ``PriorityScore.breakdown``.

    Its shape is the contract in ``DATA_MODEL`` §6.3 and the UI reads it directly. If the
    breakdown does not explain the number, the breakdown is the bug — which is why ``base``,
    ``modifier_total`` and ``value`` are all stored rather than recomputed by the reader.
    """

    model_config = ConfigDict(frozen=True)

    policy_version: str
    value: Decimal
    base: Decimal
    modifier_total: Decimal
    signals: tuple[SignalContribution, ...]
    modifiers: tuple[ModifierApplied, ...]
    flags: tuple[str, ...]
    computed_at: datetime

    def as_document(self) -> dict[str, object]:
        """Render the breakdown as the JSON document persisted in the JSONB column.

        Decimals are emitted as JSON numbers rather than pydantic's default quoted strings: the
        frontend sorts ``signals`` by ``contribution`` to justify a rank, and sorting strings
        would put ``"9.0"`` above ``"25.0"``.

        Returns:
            The mapping described in ``DATA_MODEL`` §6.3, ready for a ``JSONField``.
        """
        return {
            "policy_version": self.policy_version,
            "value": float(self.value),
            "base": float(self.base),
            "modifier_total": float(self.modifier_total),
            "signals": [
                {
                    "code": signal.code,
                    "raw": signal.raw,
                    "weight": float(signal.weight),
                    "contribution": float(signal.contribution),
                    "reason": signal.reason,
                }
                for signal in self.signals
            ],
            "modifiers": [
                {"code": modifier.code, "factor": float(modifier.factor), "reason": modifier.reason}
                for modifier in self.modifiers
            ],
            "flags": list(self.flags),
            "computed_at": self.computed_at.isoformat(),
        }
