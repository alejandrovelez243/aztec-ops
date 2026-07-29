"""The one reactor of the prioritization context: the ranking engine.

It is registered here, in the context that owns ``PriorityScore``, and not in ``apps.events``. The
bus knows a topic and a handler name; it knows nothing about scoring, which is what lets a signal
be added without the transport learning anything.

**One handler, where there used to be two.** The risk evaluator is gone with the table it wrote
(ADR 0011): risk flags are computed on read, so there is no moment at which they "change", no
previous set to diff against and therefore no event to emit. What the browser used to learn from
``project.risk.changed`` it now reads in every project payload, correct as of the moment it asked.

**The subscription is the write side only.** ``project.priority.recalculated`` is emitted from here
and is deliberately absent from :data:`ENGINE_TOPICS`: a handler that consumed what it emits would
recompute forever. The graph is acyclic by construction, not by a guard somewhere downstream.

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
    TOPIC_PROJECT_STATE_CHANGED,
    TOPIC_PROJECT_UPDATED,
    TOPIC_TASK_ARCHIVE_CHANGED,
    TOPIC_TASK_CREATED,
    TOPIC_TASK_STATE_CHANGED,
    TOPIC_TASK_UPDATED,
    EventEnvelope,
)
from apps.events.domain.routing import project_code_of, tick_at_of
from apps.events.registry import register_handler
from apps.events.services import enqueue_event
from apps.prioritization.domain.events import ORIGIN_POLICY, PriorityRecalculatedPayload
from apps.prioritization.domain.types import ScoreBreakdown, SignalContribution
from apps.prioritization.models import PriorityScore
from apps.prioritization.services import recompute_for_project

logger = logging.getLogger(__name__)

#: Registered name of the ranking handler, and therefore the ``handler`` half of every
#: ``ProcessedEvent`` row it writes. Renaming it replays the whole backlog for it.
PRIORITY_RECALCULATOR = "priority-recalculator"

#: Everything that can change a project's rank: every topic about the work itself, plus the clock.
#:
#: ``note.added`` is in here even though a note changes no field — a note is activity, so it resets
#: the ``staleness`` signal, and a portfolio where writing a note did not move the score would rank
#: an actively-managed project as abandoned. The one derived topic is excluded: see the module
#: docstring.
#:
#: The ``member.*`` topics are excluded, and the reason is the same one ADR 0011 gives for risk
#: flags. Raising somebody's capacity changes whether they are overloaded, and being overloaded is
#: a ``OWNER_OVERLOADED`` *flag* — computed on read from the current task rows, never stored — so
#: there is nothing for a recomputation to persist. Subscribing anyway would rescore every project
#: that person owns on every roster edit, to arrive at the same number: cost with no consequence.
#: What it must never do is lower those scores, because being short-staffed is a staffing decision
#: and not a reason for the work to matter less (ARCHITECTURE §4.1).
ENGINE_TOPICS: Final[frozenset[str]] = frozenset(
    {
        TOPIC_PROJECT_CREATED,
        TOPIC_PROJECT_UPDATED,
        TOPIC_PROJECT_STATE_CHANGED,
        TOPIC_TASK_CREATED,
        TOPIC_TASK_UPDATED,
        TOPIC_TASK_STATE_CHANGED,
        # Removing a task takes it out of every count the engine reads — open, overdue, urgent,
        # blocked — so a portfolio where deleting the last overdue task left the score claiming
        # overdue work would rank a cleaned-up project as a late one. Restoring it puts them back.
        TOPIC_TASK_ARCHIVE_CHANGED,
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


def _affected(envelope: EventEnvelope) -> Iterator[tuple[str, datetime]]:
    """The projects an event asks the engine to reconsider, each with the instant to apply it at.

    A generator rather than an inline branch, so "which projects does a tick touch" has a single
    definition and the two shapes of event — a fact about one project, a tick about many — reach
    the same rescoring code path.

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
