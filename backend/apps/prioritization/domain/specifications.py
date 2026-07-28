"""The composable risk specifications and the evaluator that turns them into flags.

Each condition is one class deciding one thing from facts it is handed. Composition
(``IsOverdue() & ~IsBlocked()``) replaces the nested ``if`` that would otherwise have to be
edited every time a seventh criterion appears, and health is derived from the resulting flags —
there is no editable health field anywhere in this system.
"""

from abc import ABC, abstractmethod

from .registry import register_risk, registered_risk_specifications
from .types import Health, ProjectRiskInput, RiskFlag, Severity

#: ``WorkflowState.category`` value every rule branches on. Logic never compares a state ``code``
#: or a Spanish label, both of which the operation edits from the admin.
BLOCKED_CATEGORY = "BLOCKED"


class Specification(ABC):
    """Base class for a pure, total risk condition.

    Total means it answers for any ``ProjectRiskInput`` — a subclass may not raise where its
    siblings return ``False``, narrow the accepted input, or need a database the others do not.
    That contract is what makes the operators below safe to compose in any order.
    """

    @abstractmethod
    def is_satisfied_by(self, data: ProjectRiskInput) -> bool:
        """Whether the condition holds for these facts."""

    @abstractmethod
    def detail(self, data: ProjectRiskInput) -> str:
        """The fact that satisfied the condition, in one English sentence."""

    def __and__(self, other: "Specification") -> "Specification":
        """Both conditions must hold."""
        return AndSpecification(self, other)

    def __or__(self, other: "Specification") -> "Specification":
        """Either condition may hold."""
        return OrSpecification(self, other)

    def __invert__(self) -> "Specification":
        """The condition must not hold."""
        return NotSpecification(self)


class AndSpecification(Specification):
    """Conjunction of two specifications, with both details reported when it holds."""

    def __init__(self, left: Specification, right: Specification) -> None:
        self.left = left
        self.right = right

    def is_satisfied_by(self, data: ProjectRiskInput) -> bool:
        """Whether both operands hold."""
        return self.left.is_satisfied_by(data) and self.right.is_satisfied_by(data)

    def detail(self, data: ProjectRiskInput) -> str:
        """Both operand details, since a conjunction is only explained by both halves."""
        return f"{self.left.detail(data)} {self.right.detail(data)}"


class OrSpecification(Specification):
    """Disjunction of two specifications, reporting the detail of the operand that held."""

    def __init__(self, left: Specification, right: Specification) -> None:
        self.left = left
        self.right = right

    def is_satisfied_by(self, data: ProjectRiskInput) -> bool:
        """Whether either operand holds."""
        return self.left.is_satisfied_by(data) or self.right.is_satisfied_by(data)

    def detail(self, data: ProjectRiskInput) -> str:
        """The detail of the first operand that holds, so the reason names the actual cause."""
        if self.left.is_satisfied_by(data):
            return self.left.detail(data)
        return self.right.detail(data)


class NotSpecification(Specification):
    """Negation of a specification."""

    def __init__(self, operand: Specification) -> None:
        self.operand = operand

    def is_satisfied_by(self, data: ProjectRiskInput) -> bool:
        """Whether the operand does not hold."""
        return not self.operand.is_satisfied_by(data)

    def detail(self, data: ProjectRiskInput) -> str:
        """Names the absence, which is what a negation actually asserts."""
        return f"Not the case that: {self.operand.detail(data)}"


@register_risk(flag_code="BLOCKED", severity=Severity.CRITICAL)
class IsBlocked(Specification):
    """Blocked by its own state, by an open ``Blocker`` row, or by a blocked task.

    Three sources, one flag, because the operation experiences all three identically: work is not
    moving. The state test reads ``category``, so adding ``en_espera_cliente`` in the admin makes
    this specification correct on the next event with no code change.
    """

    def is_satisfied_by(self, data: ProjectRiskInput) -> bool:
        """Whether anything is currently preventing work from moving."""
        return (
            data.state_category == BLOCKED_CATEGORY
            or data.open_blocker_count > 0
            or data.blocked_task_count > 0
        )

    def detail(self, data: ProjectRiskInput) -> str:
        """Names which of the three sources is blocking, and for how long where known."""
        if data.open_blocker_count > 0:
            age = data.oldest_blocker_age_days or 0
            return f"{data.open_blocker_count} open blocker(s); the oldest for {age} day(s)."
        if data.blocked_task_count > 0:
            return f"{data.blocked_task_count} task(s) sit in a blocked state."
        return "The project's own state is in the BLOCKED category."


@register_risk(flag_code="OVERDUE", severity=Severity.HIGH)
class IsOverdue(Specification):
    """Past its target date, or carrying tasks past their own due dates.

    Task-level lateness counts even when the project date still holds: a project that will miss
    its date is worth surfacing before the date proves it.
    """

    def is_satisfied_by(self, data: ProjectRiskInput) -> bool:
        """Whether the project or any of its open tasks has already slipped."""
        if data.overdue_task_count > 0:
            return True
        return data.target_date is not None and data.target_date < data.now.date()

    def detail(self, data: ProjectRiskInput) -> str:
        """Names the days past target, or the count of late tasks."""
        if data.target_date is not None and data.target_date < data.now.date():
            days = (data.now.date() - data.target_date).days
            return f"{days} day(s) past the target date {data.target_date.isoformat()}."
        return f"{data.overdue_task_count} task(s) past their due date."


@register_risk(flag_code="NO_NEXT_STEP", severity=Severity.MEDIUM)
class HasNoNextStep(Specification):
    """No recorded ``next_step`` and no task in progress.

    Either one alone is fine — a project with work underway does not need a written next step, and
    a written next step covers a project between tasks. The absence of both is the condition the
    command center's "no clear next step" panel exists to show.
    """

    def is_satisfied_by(self, data: ProjectRiskInput) -> bool:
        """Whether nobody can say what happens next on this project."""
        return not data.next_step.strip() and not data.has_in_progress_task

    def detail(self, data: ProjectRiskInput) -> str:  # noqa: ARG002
        """States the absence; there is no number to quote for a missing plan."""
        return "No next step is recorded and no task is in progress."


@register_risk(flag_code="NO_TARGET_DATE", severity=Severity.MEDIUM)
class HasNoTargetDate(Specification):
    """An active project with no committed target date.

    Five of the twenty-two source projects are in this state. The date is never backfilled with
    today or a sentinel: the missing commitment is the finding, and hiding it would also silently
    change ``deadline_pressure`` from 0.5 to something invented.
    """

    def is_satisfied_by(self, data: ProjectRiskInput) -> bool:
        """Whether an active project has committed to no date at all."""
        return data.target_date is None

    def detail(self, data: ProjectRiskInput) -> str:  # noqa: ARG002
        """States the absence of a committed date."""
        return "No target date is committed."


@register_risk(flag_code="STALE", severity=Severity.MEDIUM)
class IsStale(Specification):
    """No recorded activity for at least the configured threshold.

    Threshold is operational (``STALENESS_THRESHOLD_DAYS``, default 14) and arrives on the input.
    A project that has never recorded activity satisfies this too — never is longer than any
    threshold, and treating an empty history as fresh would silence exactly the abandoned work.
    """

    def is_satisfied_by(self, data: ProjectRiskInput) -> bool:
        """Whether the project has been silent for longer than the operation accepts."""
        if data.days_since_last_activity is None:
            return True
        return data.days_since_last_activity >= data.staleness_threshold_days

    def detail(self, data: ProjectRiskInput) -> str:
        """Names the days of silence against the threshold."""
        if data.days_since_last_activity is None:
            return "No activity has ever been recorded."
        return (
            f"No activity for {data.days_since_last_activity} day(s), threshold is "
            f"{data.staleness_threshold_days} day(s)."
        )


@register_risk(flag_code="OWNER_OVERLOADED", severity=Severity.HIGH)
class OwnerOverloaded(Specification):
    """The owner's computed load exceeds their weekly capacity.

    Reads the load it is handed; it does not count tasks. Raising this flag never lowers the
    project's score: priority belongs to the work, not to who happens to be free, and lowering it
    would hide the bottleneck at precisely the moment it is deciding the week. An unowned project
    cannot satisfy this — that absence is the ``NO_NEXT_STEP`` and staffing conversation instead.
    """

    def is_satisfied_by(self, data: ProjectRiskInput) -> bool:
        """Whether the owner is carrying more than their stated capacity."""
        if not data.owner_code:
            return False
        return data.owner_load_points > data.owner_capacity_points

    def detail(self, data: ProjectRiskInput) -> str:
        """Names the load, the capacity and who carries it."""
        return (
            f"Owner {data.owner_code} carries {data.owner_load_points} point(s) against a "
            f"capacity of {data.owner_capacity_points}."
        )


def evaluate_risk(data: ProjectRiskInput) -> tuple[RiskFlag, ...]:
    """Run every registered specification and return the flags that hold.

    Archived projects return no flags at all: they are out of the queue, and flagging them would
    fill the panels the command center uses to decide today's work with projects nobody will act
    on. That exclusion is one guard here rather than a repeated condition inside six classes.

    Args:
        data: The facts for one project, including ``now``.

    Returns:
        The satisfied flags in registration order, each carrying the severity from its registry
        entry and the detail its specification produced.
    """
    if data.is_archived:
        return ()

    return tuple(
        RiskFlag(
            code=entry.flag_code,
            severity=entry.severity,
            detail=entry.specification.detail(data),
        )
        for entry in registered_risk_specifications()
        if entry.specification.is_satisfied_by(data)
    )


def derive_health(flags: tuple[RiskFlag, ...]) -> Health:
    """Derive project health from the raised flags.

    ``BLOCKED`` when any ``CRITICAL`` flag is raised, ``AT_RISK`` when any flag is raised at all,
    otherwise ``HEALTHY``. Derived rather than stored so health can never disagree with the flags
    a reader is looking at, and so the source spreadsheet's ``imported_health`` stays a
    cross-check instead of a second source of truth.

    Args:
        flags: The currently raised flags for one project.

    Returns:
        The health value the read model copies into ``ProjectSnapshot.health``.
    """
    if any(flag.severity == Severity.CRITICAL for flag in flags):
        return Health.BLOCKED
    if flags:
        return Health.AT_RISK
    return Health.HEALTHY
