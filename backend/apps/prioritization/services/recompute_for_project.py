"""Use case: recompute one project's score and risk flags.

Ends at the database. It writes no ``ActivityRecord`` and no ``OutboxEvent``: the caller — the
``priority-recalculator`` consumer — decides what to record and what to publish, and it only does
so when this function reports that something actually changed. That is what keeps a clock tick on
a quiet portfolio from producing twenty-two identical events every five minutes.
"""

from datetime import datetime
from decimal import Decimal

from django.db import transaction
from pydantic import BaseModel, ConfigDict

from ..domain.errors import ActivePolicyNotFound
from ..domain.policies import load_policy
from ..domain.scoring import compute_breakdown, compute_input_hash, compute_valid_until
from ..domain.specifications import derive_health, evaluate_risk
from ..domain.types import Health, RiskFlag
from ..models import PriorityPolicy, PriorityScore
from ..models import RiskFlag as RiskFlagRow
from .collect_project_facts import collect_project_facts


class RecomputeResult(BaseModel):
    """What the recomputation did, in the terms the caller needs to decide whether to emit.

    ``changed`` is the whole point: under at-least-once delivery the same event arrives twice and
    the second recomputation must be observable as a no-op, not as a second
    ``project.priority.recalculated`` reaching every open browser.
    """

    model_config = ConfigDict(frozen=True)

    project_code: str
    changed: bool
    value: Decimal
    previous_value: Decimal | None
    policy_version: str
    health: Health
    raised_flags: tuple[str, ...]
    cleared_flags: tuple[str, ...]
    valid_until: datetime | None


@transaction.atomic
def recompute_for_project(*, project_code: str, now: datetime) -> RecomputeResult:
    """Score one project, reconcile its risk flags, and report whether anything moved.

    The score row and the flag rows are written in one transaction, so a reader never sees a
    score justified by flags that were not persisted. Recomputation short-circuits when the input
    hash and the policy version both match the stored row — same facts, same criterion, same
    number — which is what makes the recalculator cheap under duplicate delivery.

    Args:
        project_code: Business code, e.g. ``"PRJ-01"``.
        now: The instant to score for; also the ``detected_at`` / ``cleared_at`` of any flag
            movement, so a replay does not invent a new time.

    Returns:
        The new value, the previous one, the flags that moved, the derived health and the next
        instant a time-dependent signal can change bucket.

    Raises:
        ActivePolicyNotFound: No policy is active; the engine refuses to rank against an implicit
            criterion.
        ProjectNotFound: The code names no project.
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
            health=derive_health(flags),
            raised_flags=(),
            cleared_flags=(),
            valid_until=stored.valid_until,
        )

    valid_until = compute_valid_until(data)
    PriorityScore.objects.update_or_create(
        project_id=facts.project_id,
        defaults={
            "value": breakdown.value,
            "policy_version": breakdown.policy_version,
            "breakdown": breakdown.as_document(),
            "modifier_total": breakdown.modifier_total,
            "computed_at": now,
            "input_hash": input_hash,
            "valid_until": valid_until,
        },
    )

    raised, cleared = _reconcile_flags(project_id=facts.project_id, flags=flags, now=now)

    return RecomputeResult(
        project_code=project_code,
        changed=previous_value != breakdown.value or bool(raised) or bool(cleared),
        value=breakdown.value,
        previous_value=previous_value,
        policy_version=policy.version,
        health=derive_health(flags),
        raised_flags=raised,
        cleared_flags=cleared,
        valid_until=valid_until,
    )


def _reconcile_flags(
    *, project_id: int, flags: tuple[RiskFlag, ...], now: datetime
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Bring the persisted flags in line with the evaluation, preserving each open episode.

    A flag that is still satisfied keeps its original ``detected_at`` — that is what lets the UI
    say "blocked for 19 days" — and only its ``detail`` is refreshed. A flag that stops being
    satisfied is cleared rather than deleted, and raising it again creates a new row, so the risk
    history stays reconstructible.
    """
    satisfied = {flag.code: flag for flag in flags}
    open_rows = {
        row.code: row for row in RiskFlagRow.objects.for_project(project_id).open().oldest_first()
    }

    cleared: list[str] = []
    for code, stale_row in open_rows.items():
        if code in satisfied:
            continue
        stale_row.cleared_at = now
        stale_row.save(update_fields=["cleared_at"])
        cleared.append(code)

    raised: list[str] = []
    for code, flag in satisfied.items():
        open_row = open_rows.get(code)
        if open_row is None:
            RiskFlagRow.objects.create(
                project_id=project_id,
                code=flag.code,
                severity=flag.severity.value,
                detail=flag.detail,
                detected_at=now,
            )
            raised.append(code)
            continue
        if open_row.detail != flag.detail or open_row.severity != flag.severity.value:
            open_row.detail = flag.detail
            open_row.severity = flag.severity.value
            open_row.save(update_fields=["detail", "severity"])

    return tuple(raised), tuple(cleared)
