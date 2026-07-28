"""The two reactors of the prioritization context: the ranking engine and the risk engine.

Both are registered here, in the context that owns ``PriorityScore`` and ``RiskFlag``, and not in
``apps.events``. The bus knows a topic and a handler name; it knows nothing about scoring, which is
what lets a signal or a specification be added without the transport learning anything.

**Two handlers and not one**, over the same subscription, because they have two reasons to react
and two tables to write. One combined handler would make a failure in the risk half roll back a
perfectly good score, and — worse — it would emit one event for two independent facts, so a client
watching the queue could not tell a reranking from a health change. One writer per table, one
handler per reason to react (EVENTS.md §5).

**The subscription is the write side only.** ``project.priority.recalculated`` and
``project.risk.changed`` are emitted from here and are deliberately absent from
:data:`ENGINE_TOPICS`: a handler that consumed what it emits would recompute forever. The graph is
acyclic by construction, not by a guard somewhere downstream.

**Time comes from the envelope.** A data event is applied at its ``occurred_at`` and a tick at its
``tick_at``, never at ``timezone.now()``. That is what makes a redelivery land on the same number
instead of quietly inventing a newer one.
"""

import logging
from collections.abc import Iterator
from datetime import datetime
from decimal import Decimal
from typing import Final

from apps.activity.domain.value_objects import ActivityCommand
from apps.activity.services import write_activity
from apps.events.domain.envelope import (
    ENTITY_PROJECT,
    SYSTEM_ACTOR,
    TOPIC_BLOCKER_RAISED,
    TOPIC_BLOCKER_RESOLVED,
    TOPIC_CLOCK_TICKED,
    TOPIC_NOTE_ADDED,
    TOPIC_PROJECT_CREATED,
    TOPIC_PROJECT_PRIORITY_RECALCULATED,
    TOPIC_PROJECT_RISK_CHANGED,
    TOPIC_PROJECT_STATE_CHANGED,
    TOPIC_PROJECT_UPDATED,
    TOPIC_TASK_CREATED,
    TOPIC_TASK_STATE_CHANGED,
    TOPIC_TASK_UPDATED,
    EventEnvelope,
)
from apps.events.domain.routing import project_code_of, tick_at_of
from apps.events.registry import register_handler
from apps.events.services import enqueue_event
from apps.prioritization.domain.events import (
    ORIGIN_POLICY,
    PriorityRecalculatedPayload,
    RiskChangedPayload,
)
from apps.prioritization.domain.types import ScoreBreakdown, SignalContribution
from apps.prioritization.models import PriorityScore
from apps.prioritization.services import evaluate_risk_for_project, recompute_for_project

logger = logging.getLogger(__name__)

#: Registered name of the ranking handler, and therefore the ``handler`` half of every
#: ``ProcessedEvent`` row it writes. Renaming it replays the whole backlog for it.
PRIORITY_RECALCULATOR = "priority-recalculator"

#: Registered name of the risk handler.
RISK_EVALUATOR = "risk-evaluator"

#: Everything that can change a project's rank: every write-side topic, plus the clock.
#:
#: ``note.added`` is in here even though a note changes no field — a note is activity, so it resets
#: the ``staleness`` signal, and a portfolio where writing a note did not move the score would rank
#: an actively-managed project as abandoned. The two derived topics are excluded: see the module
#: docstring.
ENGINE_TOPICS: Final[frozenset[str]] = frozenset(
    {
        TOPIC_PROJECT_CREATED,
        TOPIC_PROJECT_UPDATED,
        TOPIC_PROJECT_STATE_CHANGED,
        TOPIC_TASK_CREATED,
        TOPIC_TASK_UPDATED,
        TOPIC_TASK_STATE_CHANGED,
        TOPIC_BLOCKER_RAISED,
        TOPIC_BLOCKER_RESOLVED,
        TOPIC_NOTE_ADDED,
        TOPIC_CLOCK_TICKED,
    }
)

#: Verb of the audit record a recomputation writes. The same verb a manual override writes, on
#: purpose: the timeline's "why did this move" filter must not miss half the answer, and ``origin``
#: is what separates the engine's arithmetic from a person's decision.
VERB_PRIORITY_CHANGED: Final[str] = "PRIORITY_CHANGED"


@register_handler(name=PRIORITY_RECALCULATOR, topics=ENGINE_TOPICS)
def recalculate_priority(envelope: EventEnvelope) -> None:
    """Rescore the projects this event affects and announce only the ones that moved.

    A data event affects exactly one project — the one named by ``entity.id`` or by
    ``payload.project_code``. A ``clock.ticked`` affects the projects whose stored ``valid_until``
    has passed the tick instant, which is normally none of them: recomputing the whole portfolio on
    every tick would work at twenty-two projects and would be the wrong shape at any other, so the
    selection is one index scan and a quiet tick emits nothing (EVENTS.md §4).

    Emission is conditional on the recomputation reporting a change, in value *or* in breakdown. A
    rank that stays at 61.4 for a materially different reason is news; a redelivery that lands on
    the same number and the same reasons is not, and forwarding it would push a frame to every open
    browser for a fact that did not happen.

    Args:
        envelope: A delivered event on one of :data:`ENGINE_TOPICS`.

    Raises:
        EventNamesNoProjectError: A non-project event carries no ``project_code``. A producer bug:
            it dead-letters the row rather than silently skipping the recomputation.
        MalformedTickError: A tick with no usable ``tick_at``, which cannot be applied
            deterministically.
        ProjectNotFound: The event names a project that no longer exists. It is raised, not
            swallowed, so the dangling reference stays visible in the admin.
        ActivePolicyNotFound: No ``PriorityPolicy`` is active. The engine refuses to rank against
            an implicit criterion; activating a policy and re-queuing the event is the fix.
    """
    for project_code, now in _affected(envelope):
        _rescore(project_code=project_code, now=now, envelope=envelope)


@register_handler(name=RISK_EVALUATOR, topics=ENGINE_TOPICS)
def evaluate_risk(envelope: EventEnvelope) -> None:
    """Re-run the risk specifications for the projects this event affects and reconcile the flags.

    Same subscription and same selection as :func:`recalculate_priority`, because risk is stale for
    exactly the same two reasons: the data moved, or the calendar did. ``OVERDUE`` in particular
    becomes true at midnight with nobody touching anything, which is the whole reason the clock is
    a participant on the bus rather than an assumption.

    ``project.risk.changed`` is emitted only when the flag set or the derived health actually
    moved. A refreshed wording — "2 tasks past due" becoming "3 tasks past due" — is the same risk
    and is deliberately not an event.

    No ``ActivityRecord`` is written here, and that is not an oversight: the closed verb set
    (ARCHITECTURE §3.4) has no risk verb, because the ``RiskFlag`` rows are themselves the
    append-only history — a flag keeps its ``detected_at`` while it stands and is cleared rather
    than deleted, so "blocked for 19 days" is answerable from the table.

    Args:
        envelope: A delivered event on one of :data:`ENGINE_TOPICS`.

    Raises:
        EventNamesNoProjectError: A non-project event carries no ``project_code``.
        MalformedTickError: A tick with no usable ``tick_at``.
        ProjectNotFound: The event names a project that no longer exists.
    """
    for project_code, now in _affected(envelope):
        _reevaluate(project_code=project_code, now=now, envelope=envelope)


def _affected(envelope: EventEnvelope) -> Iterator[tuple[str, datetime]]:
    """The projects an event asks the engines to reconsider, each with the instant to apply it at.

    One generator for both handlers, so "which projects does a tick touch" has a single definition.
    A second copy is where the two engines would start disagreeing about which projects went stale,
    and a project scored against a tick but not re-flagged against it is exactly the inconsistency
    the read model would then render.

    Args:
        envelope: The delivered event.

    Yields:
        ``(project_code, now)`` pairs. For a data event, one pair at the event's ``occurred_at``.
        For a tick, one pair per stale score at the tick's ``tick_at`` — the payload's instant and
        never the wall clock, so a redelivered tick selects the same rows and computes the same
        numbers.

    Raises:
        EventNamesNoProjectError: A non-project event carries no ``project_code``.
        MalformedTickError: The tick carries no usable instant.
    """
    if envelope.topic != TOPIC_CLOCK_TICKED:
        yield project_code_of(envelope), envelope.occurred_at
        return

    tick_at = tick_at_of(envelope)
    codes = PriorityScore.objects.stale_at(tick_at).project_codes()
    logger.debug(
        "clock tick selected the projects whose score expired",
        extra={"event_id": str(envelope.id), "projects": len(codes)},
    )
    for code in codes:
        yield code, tick_at


def _rescore(*, project_code: str, now: datetime, envelope: EventEnvelope) -> None:
    """Recompute one project, and audit plus publish only if the recomputation is news."""
    result = recompute_for_project(project_code=project_code, now=now)
    if not result.changed:
        return

    write_activity(
        ActivityCommand(
            entity_type=ENTITY_PROJECT,
            entity_id=project_code,
            verb=VERB_PRIORITY_CHANGED,
            origin=ORIGIN_POLICY,
            actor=SYSTEM_ACTOR,
            from_value=_as_text(result.previous_value),
            to_value=_as_text(result.value),
            metadata=_movement(
                breakdown=result.breakdown,
                previous_value=result.previous_value,
                trigger=envelope,
            ),
            occurred_at=now,
            correlation_id=envelope.correlation_id,
        )
    )
    enqueue_event(
        topic=TOPIC_PROJECT_PRIORITY_RECALCULATED,
        entity_type=ENTITY_PROJECT,
        entity_id=project_code,
        payload=PriorityRecalculatedPayload.of(
            breakdown=result.breakdown,
            previous_value=result.previous_value,
            origin=result.origin,
        ).model_dump(mode="json"),
        actor=SYSTEM_ACTOR,
        correlation_id=envelope.correlation_id,
        occurred_at=now,
    )


def _reevaluate(*, project_code: str, now: datetime, envelope: EventEnvelope) -> None:
    """Reconcile one project's flags, and publish only if the set or the health moved."""
    result = evaluate_risk_for_project(project_code=project_code, now=now)
    if not result.changed:
        return

    enqueue_event(
        topic=TOPIC_PROJECT_RISK_CHANGED,
        entity_type=ENTITY_PROJECT,
        entity_id=project_code,
        payload=RiskChangedPayload.of(
            flags=result.flags,
            added=result.added,
            removed=result.removed,
            health=result.health,
            previous_health=result.previous_health,
        ).model_dump(mode="json"),
        actor=SYSTEM_ACTOR,
        correlation_id=envelope.correlation_id,
        occurred_at=now,
    )


def _movement(
    *, breakdown: ScoreBreakdown, previous_value: Decimal | None, trigger: EventEnvelope
) -> dict[str, object]:
    """What the audit record says about *why* the number moved.

    Names the dominant signal rather than the one whose raw value changed most: the previous
    breakdown is not carried out of the recomputation, and re-reading the row it has already
    overwritten would report the new explanation as the old one. The dominant contribution answers
    the question the timeline is actually asked — "why is this project where it is" — and the event
    beside it carries the full breakdown for the rest.

    Args:
        breakdown: The explanation just persisted.
        previous_value: The score replaced, or ``None`` on a first computation.
        trigger: The event that caused the recomputation, recorded so a rank movement can be traced
            back to the change that produced it rather than only to the decision it belongs to.

    Returns:
        The ``metadata`` document of the ``PRIORITY_CHANGED`` record.
    """
    dominant = _dominant(breakdown)
    return {
        "origin": ORIGIN_POLICY,
        "policy_version": breakdown.policy_version,
        "signal": dominant.code if dominant is not None else None,
        "contribution": float(dominant.contribution) if dominant is not None else None,
        "delta": float(breakdown.value - previous_value) if previous_value is not None else None,
        "trigger_topic": trigger.topic,
        "trigger_event_id": str(trigger.id),
    }


def _dominant(breakdown: ScoreBreakdown) -> SignalContribution | None:
    """The signal contributing most to the score, or ``None`` for a policy with no signals."""
    if not breakdown.signals:
        return None
    return max(breakdown.signals, key=lambda signal: signal.contribution)


def _as_text(value: Decimal | None) -> str:
    """Render a score for the audit trail's text columns; the empty string means "no previous"."""
    return "" if value is None else str(value)
