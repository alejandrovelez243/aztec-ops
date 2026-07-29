"""The engine itself: weighted sum, modifiers, clamp, breakdown and ``valid_until``.

Deterministic and explainable end to end. Nothing here reads a clock, a database or a setting;
the same ``SignalInput`` and the same ``PolicyView`` always produce the same number and the same
sentences, which is what makes a rank defensible three weeks later.
"""

import hashlib
import json
from datetime import UTC, datetime, time, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Final

from . import signals as _signals  # noqa: F401  Importing populates the signal registry.
from .policies import PolicyView
from .registry import get_signal
from .types import (
    ModifierApplied,
    RiskFlag,
    ScoreBreakdown,
    SignalContribution,
    SignalInput,
)

#: The stated range of a score. Named because the clamp, the check constraint and the UI all
#: depend on the same two numbers.
MIN_SCORE: Final[Decimal] = Decimal("0")
MAX_SCORE: Final[Decimal] = Decimal("100")

#: Two decimals, matching ``numeric(5,2)`` on the column.
CENTS: Final[Decimal] = Decimal("0.01")

#: How far before the target date the "final week" bucket opens. A project crossing into its last
#: week is a materially different conversation, so it is one of the instants ``valid_until`` will
#: wake the recalculator on.
FINAL_WEEK_DAYS: Final[int] = 7


def _quantize(value: Decimal) -> Decimal:
    """Round half up to two decimals, the persisted precision."""
    return value.quantize(CENTS, rounding=ROUND_HALF_UP)


def compute_breakdown(
    *,
    data: SignalInput,
    policy: PolicyView,
    flags: tuple[RiskFlag, ...],
) -> ScoreBreakdown:
    """Score one project and produce the document that justifies the number.

    Every registered signal runs exactly once — the evaluator resolves strategies through the
    registry and never learns their names, so a seventh signal changes nothing here. The
    invariants ``contribution == round(raw * weight * 100, 2)``, ``base == sum(contribution)`` and
    ``value == clamp(round(base * modifier_total, 2))`` hold by construction rather than by
    assertion, because the breakdown is built from the same numbers that produce the value.

    Args:
        data: All facts about the project at ``data.now``.
        policy: The validated active policy; its weights cover the registry exactly.
        flags: Risk flags raised for this project, denormalized into the breakdown so the UI can
            render "the score is real, the owner is the constraint" without a second query.

    Returns:
        The frozen breakdown, whose ``value`` is the persisted score.
    """
    contributions: list[SignalContribution] = []
    for signal_code, weight in policy.weights.items():
        strategy = get_signal(signal_code)
        result = strategy.evaluate(data)
        contribution = _quantize(Decimal(str(result.score)) * weight * MAX_SCORE)
        contributions.append(
            SignalContribution(
                code=signal_code,
                label=strategy.label,
                raw=result.score,
                weight=weight,
                contribution=contribution,
                reason=result.reason,
            )
        )

    contributions.sort(key=lambda item: (-item.contribution, item.code))
    base = _quantize(sum((item.contribution for item in contributions), Decimal("0")))

    modifiers = _applied_modifiers(data, policy)
    modifier_total = Decimal("1.00")
    for modifier in modifiers:
        modifier_total *= modifier.factor

    value = min(MAX_SCORE, max(MIN_SCORE, _quantize(base * modifier_total)))

    return ScoreBreakdown(
        policy_version=policy.version,
        value=value,
        base=base,
        modifier_total=_quantize(modifier_total),
        signals=tuple(contributions),
        modifiers=modifiers,
        flags=tuple(flag.code for flag in flags),
        computed_at=data.now,
    )


def _applied_modifiers(data: SignalInput, policy: PolicyView) -> tuple[ModifierApplied, ...]:
    """Build the multiplicative adjustments this policy enables.

    Modifiers stretch the weighted sum instead of adding to it, so an engagement type that matters
    more never grants a flat bonus that would swamp the signals at the bottom of the queue.
    """
    if not policy.applies_engagement_type_modifier() or not data.engagement_type_code:
        return ()

    factor = _quantize(data.engagement_type_weight)
    return (
        ModifierApplied(
            code="engagement_type",
            factor=factor,
            # Spanish on purpose: the reason is read by the operation (CLAUDE.md §Language).
            reason=f"El tipo de engagement {data.engagement_type_code} pesa {factor}.",
        ),
    )


def compute_valid_until(data: SignalInput) -> datetime | None:
    """The earliest future instant at which a time-dependent signal changes bucket.

    Three candidates, exactly as ``DATA_MODEL`` §6.2 defines them: the target date, the start of
    the final week before it, and the instant the staleness threshold elapses. ``None`` means no
    time-derived signal can move on its own, so ``clock.ticked`` will never select this project
    and a quiet tick costs one index scan over the ``valid_until`` index.

    Args:
        data: The facts, including ``now``; candidates already in the past are discarded here so
            the recalculator never stores an instant it would immediately re-select.

    Returns:
        The earliest future bucket boundary, or ``None`` when there is none.
    """
    candidates: list[datetime] = []

    if data.target_date is not None:
        target_instant = datetime.combine(data.target_date, time.min, tzinfo=UTC)
        candidates.append(target_instant)
        candidates.append(target_instant - timedelta(days=FINAL_WEEK_DAYS))

    if data.last_activity_at is not None:
        candidates.append(data.last_activity_at + timedelta(days=data.staleness_threshold_days))

    future = [candidate for candidate in candidates if candidate > data.now]
    return min(future) if future else None


def compute_input_hash(data: SignalInput) -> str:
    """Fingerprint the facts a score was computed from.

    Equal hash plus equal ``policy_version`` means recomputation is a no-op, which is what makes
    the recalculator cheap under at-least-once delivery: the same event arriving twice writes
    once. ``now`` enters at day precision only — the sub-second instant differs on every delivery
    and would defeat the whole mechanism, while the calendar day is what the time-dependent
    signals actually read.

    Args:
        data: The assembled facts.

    Returns:
        A hex sha256 digest, sized for ``varchar(64)``.
    """
    facts = data.model_dump(mode="json")
    facts["now"] = data.now.date().isoformat()
    canonical = json.dumps(facts, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
