"""Use case: recompute one project's priority score.

Ends at the database. It writes no ``ActivityRecord`` and no ``OutboxEvent``: the caller — the
``priority-recalculator`` consumer — decides what to record and what to publish, and it only does
so when this function reports that something actually changed. That is what keeps a clock tick on
a quiet portfolio from producing twenty-two identical events every five minutes.

**Risk flags are not persisted here.** The specifications are evaluated, because the breakdown
denormalizes the raised flag codes, but the ``RiskFlag`` rows are written by
``evaluate_risk_for_project`` and by nothing else. Two groups reconciling the same rows would make
the second one see its own change already applied and stay silent, so ``project.risk.changed``
would be lost exactly when the risk moved. One writer per table, one group per reason to react.
"""

from datetime import datetime
from decimal import Decimal

from django.db import transaction
from pydantic import BaseModel, ConfigDict

from ..domain.errors import ActivePolicyNotFound
from ..domain.events import ORIGIN_MANUAL, ORIGIN_POLICY, ScoreOrigin
from ..domain.policies import load_policy
from ..domain.scoring import compute_breakdown, compute_input_hash, compute_valid_until
from ..domain.specifications import evaluate_risk
from ..domain.types import ScoreBreakdown
from ..models import PriorityOverride, PriorityPolicy, PriorityScore
from .collect_project_facts import collect_project_facts

#: Key of the breakdown document that moves on every single computation. Excluded from the
#: comparison that decides whether to emit, because ``computed_at`` is *when* we looked, not *what*
#: we found: comparing it would make every redelivery and every clock tick look like a change.
_TIMESTAMP_KEY = "computed_at"


class RecomputeResult(BaseModel):
    """What the recomputation did, in the terms the caller needs to decide whether to emit.

    ``changed`` is the whole point: under at-least-once delivery the same event arrives twice and
    the second recomputation must be observable as a no-op, not as a second
    ``project.priority.recalculated`` reaching every open browser. It is true when the value moved
    **or** when the breakdown did — a rank that stays at 61.4 for a materially different reason is
    news to the operator reading the reason.

    ``breakdown`` is carried out whole rather than as the persisted document so the caller can
    build its payload without re-reading the row it just wrote.
    """

    model_config = ConfigDict(frozen=True)

    project_code: str
    changed: bool
    value: Decimal
    previous_value: Decimal | None
    policy_version: str
    origin: ScoreOrigin
    breakdown: ScoreBreakdown
    valid_until: datetime | None


@transaction.atomic
def recompute_for_project(*, project_code: str, now: datetime) -> RecomputeResult:
    """Score one project against the active policy and report whether anything moved.

    Recomputation short-circuits when the input hash and the policy version both match the stored
    row — same facts, same criterion, same number — which is what makes the recalculator cheap
    under duplicate delivery: the second arrival of an event reads one row and writes none.

    Args:
        project_code: Business code, e.g. ``"PRJ-01"``.
        now: The instant to score for. Supplied by the caller from the envelope, never read from
            the clock, so a replayed event reproduces the score that was correct then instead of
            inventing a new one.

    Returns:
        The new value, the previous one, the breakdown that justifies it, whether any of that
        moved, and the next instant a time-dependent signal can change bucket.

    Raises:
        ActivePolicyNotFound: No policy is active; the engine refuses to rank against an implicit
            criterion.
        ProjectNotFound: The code names no project — for a consumer, an event about an aggregate
            that no longer exists, which retrying will not fix.
        PolicySignalMismatch: The active policy and the signal registry disagree.
        PolicyWeightsNotNormalized: The active policy's weights do not sum to 1.0.
    """
    policy_row = PriorityPolicy.objects.active().first()
    if policy_row is None:
        raise ActivePolicyNotFound

    policy = load_policy(
        version=policy_row.version,
        weights=policy_row.weights,
        modifiers=policy_row.modifiers,
    )

    facts = collect_project_facts(project_code=project_code, now=now)
    data = facts.signal_input
    flags = evaluate_risk(data.as_risk_input())
    breakdown = compute_breakdown(data=data, policy=policy, flags=flags)
    input_hash = compute_input_hash(data)
    origin = _origin(project_id=facts.project_id, now=now)

    stored = PriorityScore.objects.for_project(facts.project_id).first()
    previous_value = stored.value if stored is not None else None
    if (
        stored is not None
        and stored.input_hash == input_hash
        and stored.policy_version == policy.version
    ):
        return RecomputeResult(
            project_code=project_code,
            changed=False,
            value=stored.value,
            previous_value=previous_value,
            policy_version=policy.version,
            origin=origin,
            breakdown=breakdown,
            valid_until=stored.valid_until,
        )

    document = breakdown.as_document()
    valid_until = compute_valid_until(data)
    PriorityScore.objects.update_or_create(
        project_id=facts.project_id,
        defaults={
            "value": breakdown.value,
            "policy_version": breakdown.policy_version,
            "breakdown": document,
            "modifier_total": breakdown.modifier_total,
            "computed_at": now,
            "input_hash": input_hash,
            "valid_until": valid_until,
        },
    )

    return RecomputeResult(
        project_code=project_code,
        changed=_moved(stored=stored, value=breakdown.value, document=document),
        value=breakdown.value,
        previous_value=previous_value,
        policy_version=policy.version,
        origin=origin,
        breakdown=breakdown,
        valid_until=valid_until,
    )


def _moved(*, stored: PriorityScore | None, value: Decimal, document: dict[str, object]) -> bool:
    """Whether this computation is news, rather than the same answer reached again.

    A first computation is always news. Afterwards the number and the explanation are both
    compared, because the UI renders both — but ``computed_at`` is stripped from the comparison,
    since it differs on every delivery and would report every quiet tick as a change.
    """
    if stored is None:
        return True
    if stored.value != value:
        return True
    return _without_timestamp(stored.breakdown) != _without_timestamp(document)


def _without_timestamp(document: object) -> dict[str, object]:
    """The comparable part of a breakdown document, tolerating a row written before this shape."""
    if not isinstance(document, dict):
        return {}
    return {key: value for key, value in document.items() if key != _TIMESTAMP_KEY}


def _origin(*, project_id: int, now: datetime) -> ScoreOrigin:
    """Whether a human is currently forcing this project's position.

    Expiry is evaluated here rather than inside the named query: a query must not read a clock its
    caller did not choose, or a replayed event would resolve the override against the wrong
    instant.
    """
    override = PriorityOverride.objects.for_project(project_id).live().first()
    if override is None:
        return ORIGIN_POLICY
    if override.expires_at is not None and override.expires_at <= now:
        return ORIGIN_POLICY
    return ORIGIN_MANUAL
