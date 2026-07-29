"""The two registries the engine iterates: signal strategies and risk specifications.

Adding a signal is one class plus one ``@register`` line; adding a risk criterion is one class
plus one ``@register_risk`` line. The evaluator never learns a signal's name, so nothing that
already works is opened when the seventh arrives.

These are registries with a fixed, validated key space — not a service locator. The keys are
checked against the active policy at load time, so a typo fails loudly instead of resolving to
nothing at scoring time.
"""

from collections.abc import Callable, Mapping
from typing import Protocol, runtime_checkable

from .errors import (
    RiskFlagAlreadyRegistered,
    SignalAlreadyRegistered,
    SignalNotRegistered,
)
from .types import ProjectRiskInput, Severity, SignalInput, SignalResult


@runtime_checkable
class SignalStrategy(Protocol):
    """One normalized reading of one operational fact.

    Implementations are pure: same ``SignalInput``, same ``SignalResult``, no clock, no database.
    Normalization belongs to the strategy; weighting never does — the weight lives in the active
    ``PriorityPolicy`` so the operation can rebalance the ranking without a deploy.

    A strategy also names itself: ``label`` travels into the breakdown and onto the wire, so adding
    a signal stays one class plus one registry line (CLAUDE.md rule 8) and needs no frontend
    change. Declared here rather than in a table keyed by code, because a mapping would be a second
    place to edit and a missing entry there would surface as an unnamed row in the queue instead of
    as a type error at registration.
    """

    #: Spanish name of what the signal measures, published with every breakdown line. A required
    #: member: a signal that cannot name itself reaches the UI as a bare code, which is the defect
    #: the field exists to prevent.
    label: str

    def evaluate(self, data: SignalInput) -> SignalResult:
        """Read the fact this signal measures and justify the reading."""
        ...


class RiskSpecification(Protocol):
    """A risk condition that decides, and explains, but never queries.

    Total and side-effect free over any ``ProjectRiskInput``: that is what makes
    ``IsBlocked() & ~HasNoTargetDate()`` safe to write and what keeps the tests database-free.
    """

    #: Spanish name of the raised risk, published with every flag so the client renders a chip it
    #: has never heard of. A required member: a specification that cannot name itself would reach
    #: the UI as a bare code, which is the defect the field exists to prevent.
    label: str

    def is_satisfied_by(self, data: ProjectRiskInput) -> bool:
        """Whether the condition holds for these facts."""
        ...

    def detail(self, data: ProjectRiskInput) -> str:
        """The fact that satisfied the condition, e.g. "6 días de retraso sobre la fecha…"."""
        ...


class RiskSpecificationEntry:
    """A registered specification together with the flag it raises.

    ``flag_code`` and ``severity`` live here rather than on the persisted row so that
    re-classifying a risk is a reviewed code change, not a data edit that leaves historical rows
    disagreeing with current ones.
    """

    def __init__(
        self, flag_code: str, severity: Severity, specification: RiskSpecification
    ) -> None:
        self.flag_code = flag_code
        self.severity = severity
        self.specification = specification


_SIGNALS: dict[str, SignalStrategy] = {}
_RISKS: dict[str, RiskSpecificationEntry] = {}


def register[StrategyT: type[SignalStrategy]](code: str) -> Callable[[StrategyT], StrategyT]:
    """Register a signal strategy class under a stable code.

    The code is the key inside ``PriorityPolicy.weights`` and inside every persisted
    ``breakdown``, so it is frozen once a score has been written with it. Registration
    instantiates the class immediately: strategies are stateless, and a single instance makes the
    registry cheap to iterate.

    Args:
        code: Stable signal code, e.g. ``"deadline_pressure"``.

    Returns:
        The class decorator, which returns the class unchanged.

    Raises:
        SignalAlreadyRegistered: Another strategy already claims this code.
    """

    def decorate(strategy_class: StrategyT) -> StrategyT:
        if code in _SIGNALS:
            raise SignalAlreadyRegistered(code)
        _SIGNALS[code] = strategy_class()
        return strategy_class

    return decorate


def register_risk[SpecificationT: type[RiskSpecification]](
    *, flag_code: str, severity: Severity
) -> Callable[[SpecificationT], SpecificationT]:
    """Register a risk specification class under the flag code it raises.

    Args:
        flag_code: The code every flag this specification raises carries on the wire, e.g.
            ``"NO_TARGET_DATE"``. It names no column: flags are computed on read (ADR 0011).
        severity: Severity carried by every flag this specification raises.

    Returns:
        The class decorator, which returns the class unchanged.

    Raises:
        RiskFlagAlreadyRegistered: Another specification already claims this flag code.
    """

    def decorate(specification_class: SpecificationT) -> SpecificationT:
        if flag_code in _RISKS:
            raise RiskFlagAlreadyRegistered(flag_code)
        _RISKS[flag_code] = RiskSpecificationEntry(flag_code, severity, specification_class())
        return specification_class

    return decorate


def registered_signals() -> Mapping[str, SignalStrategy]:
    """Every registered signal strategy, keyed by code.

    Returns:
        A read-only view; the evaluator iterates it and the policy loader validates its key set
        against the policy weights.
    """
    return dict(_SIGNALS)


def get_signal(code: str) -> SignalStrategy:
    """Resolve one strategy by code.

    Args:
        code: A registered signal code.

    Returns:
        The strategy instance registered under ``code``.

    Raises:
        SignalNotRegistered: Nothing is registered under that code, which for a policy weight
            means the policy references a signal this process does not implement.
    """
    strategy = _SIGNALS.get(code)
    if strategy is None:
        raise SignalNotRegistered(code)
    return strategy


def signal_label(code: str) -> str | None:
    """The label of the strategy registered under ``code``, if one still claims it.

    ``None`` rather than a raise, unlike :func:`get_signal`: the caller is reading a persisted
    ``breakdown``, where a code whose strategy has since been retired is a fact about the past and
    not a misconfiguration of the present. Scoring keeps the strict lookup.

    Args:
        code: A signal code read out of a stored document, possibly an obsolete one.

    Returns:
        The registered strategy's label, or ``None`` when nothing is registered under ``code``.
    """
    strategy = _SIGNALS.get(code)
    return strategy.label if strategy is not None else None


def registered_risk_specifications() -> tuple[RiskSpecificationEntry, ...]:
    """Every registered risk specification, in registration order.

    Returns:
        The entries the risk evaluator runs. Order is stable so two evaluations of the same
        project produce the flags in the same sequence.
    """
    return tuple(_RISKS.values())
