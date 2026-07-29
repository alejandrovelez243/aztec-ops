"""The composable risk specifications and the evaluator that turns them into flags.

Each condition is one class deciding one thing from facts it is handed. Composition
(``IsOverdue() & ~IsBlocked()``) replaces the nested ``if`` that would otherwise have to be
edited every time a seventh criterion appears, and health is derived from the resulting flags —
there is no editable health field anywhere in this system.

Every ``label`` and every string ``detail`` returns is **Spanish on purpose**: the interface is
Spanish (PRODUCT.md) and the frontend is forbidden from holding an object literal keyed by a flag
code (`docs/standards/FRONTEND.md` §7), so the words have to arrive from here. That is the same
exception the database's user-facing labels already have — identifiers, class names, docstrings,
comments and tests stay English (CLAUDE.md §Language). Do not "fix" these back.
"""

from abc import ABC, abstractmethod

from .registry import register_risk, registered_risk_specifications
from .types import Health, ProjectRiskInput, RiskFlag, Severity

#: ``WorkflowState.category`` value every rule branches on. Logic never compares a state ``code``
#: or a Spanish label, both of which the operation edits from the admin.
BLOCKED_CATEGORY = "BLOCKED"


def _counted(quantity: int, singular: str, plural: str) -> str:
    """Render a quantity with the noun agreeing in number.

    Spanish inflects the adjective as well as the noun, so ``"1 bloqueos abiertos"`` — the shape a
    naive f-string produces — is wrong twice. Both forms are written out by the caller rather than
    derived by appending an ``s``, because "tarea pasó" / "tareas pasaron" is not a suffix rule.

    Args:
        quantity: The number to render.
        singular: The noun phrase for exactly one, e.g. ``"bloqueo abierto"``.
        plural: The noun phrase for anything else, e.g. ``"bloqueos abiertos"``.

    Returns:
        The quantity and the agreeing phrase, e.g. ``"3 bloqueos abiertos"``.
    """
    return f"{quantity} {singular if quantity == 1 else plural}"


class Specification(ABC):
    """Base class for a pure, total risk condition.

    Total means it answers for any ``ProjectRiskInput`` — a subclass may not raise where its
    siblings return ``False``, narrow the accepted input, or need a database the others do not.
    That contract is what makes the operators below safe to compose in any order.

    A specification also names itself: ``label`` travels with the flag onto the wire, so adding a
    criterion stays one class plus one ``@register_risk`` line and the client needs no table of
    codes to render the new chip (CLAUDE.md rule 8).
    """

    #: Spanish name of the raised risk, rendered as the chip's text. Declared here rather than in a
    #: mapping so a new specification cannot ship a code nobody can name.
    label: str

    @abstractmethod
    def is_satisfied_by(self, data: ProjectRiskInput) -> bool:
        """Whether the condition holds for these facts."""

    @abstractmethod
    def detail(self, data: ProjectRiskInput) -> str:
        """The fact that satisfied the condition, in one Spanish sentence."""

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
        self.label = f"{left.label} y {right.label}"

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
        self.label = f"{left.label} o {right.label}"

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
        self.label = f"No {operand.label.lower()}"

    def is_satisfied_by(self, data: ProjectRiskInput) -> bool:
        """Whether the operand does not hold."""
        return not self.operand.is_satisfied_by(data)

    def detail(self, data: ProjectRiskInput) -> str:
        """Names the absence, which is what a negation actually asserts."""
        return f"No se cumple que: {self.operand.detail(data)}"


@register_risk(flag_code="BLOCKED", severity=Severity.CRITICAL)
class IsBlocked(Specification):
    """Blocked by its own state, by an open ``Blocker`` row, or by a blocked task.

    Three sources, one flag, because the operation experiences all three identically: work is not
    moving. The state test reads ``category``, so adding ``en_espera_cliente`` in the admin makes
    this specification correct on the next event with no code change.
    """

    label = "Bloqueado"

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
            blockers = _counted(data.open_blocker_count, "bloqueo abierto", "bloqueos abiertos")
            return f"{blockers}; el más antiguo lleva {_counted(age, 'día', 'días')}."
        if data.blocked_task_count > 0:
            tasks = _counted(data.blocked_task_count, "tarea está", "tareas están")
            return f"{tasks} en un estado bloqueado."
        return "El estado del proyecto es de categoría BLOCKED."


@register_risk(flag_code="OVERDUE", severity=Severity.HIGH)
class IsOverdue(Specification):
    """Past its target date, or carrying tasks past their own due dates.

    Task-level lateness counts even when the project date still holds: a project that will miss
    its date is worth surfacing before the date proves it.
    """

    label = "Vencido"

    def is_satisfied_by(self, data: ProjectRiskInput) -> bool:
        """Whether the project or any of its open tasks has already slipped."""
        if data.overdue_task_count > 0:
            return True
        return data.target_date is not None and data.target_date < data.now.date()

    def detail(self, data: ProjectRiskInput) -> str:
        """Names the days past target, or the count of late tasks."""
        if data.target_date is not None and data.target_date < data.now.date():
            days = (data.now.date() - data.target_date).days
            late = _counted(days, "día", "días")
            return f"{late} de retraso sobre la fecha objetivo {data.target_date.isoformat()}."
        tasks = _counted(data.overdue_task_count, "tarea pasó", "tareas pasaron")
        return f"{tasks} su fecha de vencimiento."


@register_risk(flag_code="NO_NEXT_STEP", severity=Severity.MEDIUM)
class HasNoNextStep(Specification):
    """No recorded ``next_step`` and no task in progress.

    Either one alone is fine — a project with work underway does not need a written next step, and
    a written next step covers a project between tasks. The absence of both is the condition the
    command center's "no clear next step" panel exists to show.
    """

    label = "Sin próximo paso"

    def is_satisfied_by(self, data: ProjectRiskInput) -> bool:
        """Whether nobody can say what happens next on this project."""
        return not data.next_step.strip() and not data.has_in_progress_task

    def detail(self, data: ProjectRiskInput) -> str:  # noqa: ARG002
        """States the absence; there is no number to quote for a missing plan."""
        return "No hay próximo paso registrado ni ninguna tarea en curso."


@register_risk(flag_code="NO_TARGET_DATE", severity=Severity.MEDIUM)
class HasNoTargetDate(Specification):
    """An active project with no committed target date.

    Five of the twenty-two source projects are in this state. The date is never backfilled with
    today or a sentinel: the missing commitment is the finding, and hiding it would also silently
    change ``deadline_pressure`` from 0.5 to something invented.
    """

    label = "Sin fecha objetivo"

    def is_satisfied_by(self, data: ProjectRiskInput) -> bool:
        """Whether an active project has committed to no date at all."""
        return data.target_date is None

    def detail(self, data: ProjectRiskInput) -> str:  # noqa: ARG002
        """States the absence of a committed date."""
        return "No hay fecha objetivo comprometida."


@register_risk(flag_code="STALE", severity=Severity.MEDIUM)
class IsStale(Specification):
    """No recorded activity for at least the configured threshold.

    Threshold is operational (``STALENESS_THRESHOLD_DAYS``, default 14) and arrives on the input.
    A project that has never recorded activity satisfies this too — never is longer than any
    threshold, and treating an empty history as fresh would silence exactly the abandoned work.
    """

    label = "Inactivo"

    def is_satisfied_by(self, data: ProjectRiskInput) -> bool:
        """Whether the project has been silent for longer than the operation accepts."""
        if data.days_since_last_activity is None:
            return True
        return data.days_since_last_activity >= data.staleness_threshold_days

    def detail(self, data: ProjectRiskInput) -> str:
        """Names the days of silence against the threshold."""
        if data.days_since_last_activity is None:
            return "Nunca se registró actividad."
        silence = _counted(data.days_since_last_activity, "día", "días")
        threshold = _counted(data.staleness_threshold_days, "día", "días")
        return f"Sin actividad desde hace {silence}; el umbral es de {threshold}."


@register_risk(flag_code="OWNER_OVERLOADED", severity=Severity.HIGH)
class OwnerOverloaded(Specification):
    """The owner's computed load exceeds their weekly capacity.

    Reads the load it is handed; it does not count tasks. Raising this flag never lowers the
    project's score: priority belongs to the work, not to who happens to be free, and lowering it
    would hide the bottleneck at precisely the moment it is deciding the week. An unowned project
    cannot satisfy this — that absence is the ``NO_NEXT_STEP`` and staffing conversation instead.
    """

    label = "Responsable sobrecargado"

    def is_satisfied_by(self, data: ProjectRiskInput) -> bool:
        """Whether the owner is carrying more than their stated capacity."""
        if not data.owner_code:
            return False
        return data.owner_load_points > data.owner_capacity_points

    def detail(self, data: ProjectRiskInput) -> str:
        """Names the load, the capacity and who carries it."""
        load = _counted(data.owner_load_points, "punto", "puntos")
        capacity = _counted(data.owner_capacity_points, "punto", "puntos")
        return f"{data.owner_code} acumula {load} frente a una capacidad de {capacity}."


def evaluate_risk(data: ProjectRiskInput) -> tuple[RiskFlag, ...]:
    """Run every registered specification and return the flags that hold.

    Archived projects return no flags at all: they are out of the queue, and flagging them would
    fill the panels the command center uses to decide today's work with projects nobody will act
    on. That exclusion is one guard here rather than a repeated condition inside six classes.

    Args:
        data: The facts for one project, including ``now``.

    Returns:
        The satisfied flags in registration order, each carrying the severity from its registry
        entry and the label and detail its specification produced.
    """
    if data.is_archived:
        return ()

    return tuple(
        RiskFlag(
            code=entry.flag_code,
            severity=entry.severity,
            label=entry.specification.label,
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
