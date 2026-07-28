"""Use case: re-run the risk specifications for one project and reconcile its flags.

The **only** writer of ``prioritization_riskflag``. ``recompute_for_project`` evaluates the same
specifications — the breakdown denormalizes the raised codes — but persists none of them, because
a table with two writers in two consumer groups produces exactly one failure mode: whichever group
runs second finds the change already applied, reports nothing moved, and the event that would have
told the browser the project is now blocked is never emitted.

Ends at the database. The ``OutboxEvent`` belongs to the ``risk-evaluator`` consumer, which emits
``project.risk.changed`` only when this function reports a change.
"""

from datetime import datetime

from django.db import transaction
from pydantic import BaseModel, ConfigDict

from ..domain.specifications import derive_health, evaluate_risk
from ..domain.types import Health, RiskFlag, Severity
from ..models import RiskFlag as RiskFlagRow
from .collect_project_facts import collect_project_facts


class RiskEvaluationResult(BaseModel):
    """What the evaluation found, in the terms the caller needs to decide whether to emit.

    ``flags`` is the complete set now raised, not a delta, because that is what the risk panels
    render and what an event has to carry for a client that missed a frame. ``added`` and
    ``removed`` describe the movement, and ``changed`` is their disjunction widened with the
    health transition — a flag swapped for another of the same severity moves the set without
    moving the health, and both are news.
    """

    model_config = ConfigDict(frozen=True)

    project_code: str
    changed: bool
    flags: tuple[RiskFlag, ...]
    added: tuple[str, ...]
    removed: tuple[str, ...]
    health: Health
    previous_health: Health


@transaction.atomic
def evaluate_risk_for_project(*, project_code: str, now: datetime) -> RiskEvaluationResult:
    """Evaluate every registered specification and bring the persisted flags in line.

    A flag that is still satisfied keeps its original ``detected_at`` — that is what lets the UI
    say "blocked for 19 days" — and only its ``detail`` and ``severity`` are refreshed. A flag that
    stops being satisfied is cleared rather than deleted, and raising it again inserts a new row,
    so the risk history stays reconstructible. Neither a refresh nor a re-raise of an unchanged set
    reports a change, which is what makes a redelivery silent.

    Args:
        project_code: Business code, e.g. ``"PRJ-01"``.
        now: The instant to evaluate for, and the ``detected_at`` / ``cleared_at`` of any flag that
            moves. Taken from the envelope so a replay does not invent a new time — and so "open
            for 19 days" survives a redelivery.

    Returns:
        The current flag set, what moved, the derived health and the health it replaced.

    Raises:
        ProjectNotFound: The code names no project. For a consumer this means an event about a
            deleted aggregate, which retrying will not fix.
    """
    facts = collect_project_facts(project_code=project_code, now=now)
    flags = evaluate_risk(facts.signal_input.as_risk_input())

    open_rows = {
        row.code: row
        for row in RiskFlagRow.objects.for_project(facts.project_id).open().oldest_first()
    }
    previous_health = derive_health(
        tuple(
            RiskFlag(code=row.code, severity=Severity(row.severity), detail=row.detail)
            for row in open_rows.values()
        )
    )

    satisfied = {flag.code: flag for flag in flags}
    removed = _clear_unsatisfied(open_rows=open_rows, satisfied=satisfied, now=now)
    added = _raise_new(
        project_id=facts.project_id, open_rows=open_rows, satisfied=satisfied, now=now
    )

    health = derive_health(flags)
    return RiskEvaluationResult(
        project_code=project_code,
        changed=bool(added or removed) or health != previous_health,
        flags=flags,
        added=added,
        removed=removed,
        health=health,
        previous_health=previous_health,
    )


def _clear_unsatisfied(
    *, open_rows: dict[str, RiskFlagRow], satisfied: dict[str, RiskFlag], now: datetime
) -> tuple[str, ...]:
    """Close the open episodes whose condition no longer holds, in registration-stable order."""
    cleared: list[str] = []
    for code, row in open_rows.items():
        if code in satisfied:
            continue
        row.cleared_at = now
        row.save(update_fields=["cleared_at"])
        cleared.append(code)
    return tuple(cleared)


def _raise_new(
    *,
    project_id: int,
    open_rows: dict[str, RiskFlagRow],
    satisfied: dict[str, RiskFlag],
    now: datetime,
) -> tuple[str, ...]:
    """Insert a row per newly satisfied condition and refresh the wording of the standing ones.

    A refreshed ``detail`` is deliberately not a change: "2 tasks past their due date" becoming
    "3 tasks past their due date" is the same risk, and emitting on it would push a frame to every
    browser on each task edit while the panel already re-renders from the originating event.
    """
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
    return tuple(raised)
