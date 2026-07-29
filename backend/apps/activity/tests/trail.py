"""A small, deterministic audit trail for the timeline reads to be asserted against.

Written through :func:`write_activity`, the one write port, rather than by touching the model:
these tests are about what a reader gets back, and a builder that inserted rows the write port
would have refused would let a read test pass over data the system can never actually hold.

Every timestamp is a literal. The feed is ordered by ``occurred_at`` and filtered by a window, so
a trail anchored on "now" would be a suite that reads differently on a Tuesday — and the tie case
(two facts at the same instant, ordered by insertion) can only be built when the instant is
chosen rather than observed.

Not a factory library, and deliberately so: the fixed codes below are what an assertion reads, so
``PRJ-T1`` in a failure message can be found in one place.
"""

from datetime import UTC, datetime
from uuid import UUID

from apps.activity.domain.value_objects import ActivityCommand
from apps.activity.models import ActivityRecord
from apps.activity.services import write_activity

#: The two projects, the task and the blocker the trail talks about. Business codes, never primary
#: keys: that is what lets one feed carry three contexts without a join.
PROJECT_CODE = "PRJ-T1"
OTHER_PROJECT_CODE = "PRJ-T2"
TASK_CODE = "PRJ-T1-T01"
BLOCKER_CODE = "PRJ-T1-B01"

#: The people. ``system`` is the engine, and is an actor like any other as far as the trail cares.
CAMILA = "camila"
DARIO = "dario"
SYSTEM_ACTOR = "system"

#: One reprioritization: "PRJ-T2 was raised, so PRJ-T1 was lowered to make room" is two rows that
#: only this id shows to be one decision.
DECISION_ID = UUID("11111111-1111-4111-8111-111111111111")
#: Everything else happened on its own.
CREATION_ID = UUID("22222222-2222-4222-8222-222222222222")
MOVE_ID = UUID("33333333-3333-4333-8333-333333333333")
TASK_ID = UUID("44444444-4444-4444-8444-444444444444")
BLOCKER_ID = UUID("55555555-5555-4555-8555-555555555555")

#: The five instants the trail happened at. ``PRIORITY_AT`` carries two records, which is the tie
#: the ordering has to break by insertion order rather than leave to the planner.
CREATED_AT = datetime(2026, 7, 20, 9, 0, tzinfo=UTC)
MOVED_AT = datetime(2026, 7, 21, 9, 0, tzinfo=UTC)
TASK_ADDED_AT = datetime(2026, 7, 22, 9, 0, tzinfo=UTC)
BLOCKED_AT = datetime(2026, 7, 23, 9, 0, tzinfo=UTC)
PRIORITY_AT = datetime(2026, 7, 24, 9, 0, tzinfo=UTC)


class Trail:
    """Six facts spanning two projects, a task and a blocker, addressed by attribute.

    The attributes are assigned in the order the facts happened; :meth:`newest_first` is the
    reverse of that order, with :attr:`lowered` ahead of :attr:`raised` because the two share an
    instant and ``lowered`` was written second.
    """

    def __init__(self) -> None:
        """Append the whole trail. Six inserts, cheap enough to build per test class."""
        self.created = write_activity(
            ActivityCommand(
                entity_type=ActivityRecord.EntityType.PROJECT,
                entity_id=PROJECT_CODE,
                verb=ActivityRecord.Verb.CREATED,
                origin=ActivityRecord.Origin.SYSTEM,
                actor=CAMILA,
                occurred_at=CREATED_AT,
                correlation_id=CREATION_ID,
            )
        )
        self.moved = write_activity(
            ActivityCommand(
                entity_type=ActivityRecord.EntityType.PROJECT,
                entity_id=PROJECT_CODE,
                verb=ActivityRecord.Verb.STATE_CHANGED,
                origin=ActivityRecord.Origin.MANUAL,
                actor=CAMILA,
                reason="Kickoff signed",
                occurred_at=MOVED_AT,
                correlation_id=MOVE_ID,
            )
        )
        self.task_added = write_activity(
            ActivityCommand(
                entity_type=ActivityRecord.EntityType.TASK,
                entity_id=TASK_CODE,
                verb=ActivityRecord.Verb.TASK_ADDED,
                origin=ActivityRecord.Origin.SYSTEM,
                actor=DARIO,
                occurred_at=TASK_ADDED_AT,
                correlation_id=TASK_ID,
            )
        )
        self.blocker_raised = write_activity(
            ActivityCommand(
                entity_type=ActivityRecord.EntityType.BLOCKER,
                entity_id=BLOCKER_CODE,
                verb=ActivityRecord.Verb.BLOCKER_RAISED,
                origin=ActivityRecord.Origin.MANUAL,
                actor=DARIO,
                reason="Client has not granted access",
                occurred_at=BLOCKED_AT,
                correlation_id=BLOCKER_ID,
            )
        )
        # The pair that shares an instant, written raised-then-lowered so that insertion order
        # and timestamp order disagree and the tiebreaker is actually exercised.
        self.raised = write_activity(
            ActivityCommand(
                entity_type=ActivityRecord.EntityType.PROJECT,
                entity_id=OTHER_PROJECT_CODE,
                verb=ActivityRecord.Verb.PRIORITY_CHANGED,
                origin=ActivityRecord.Origin.POLICY,
                actor=SYSTEM_ACTOR,
                occurred_at=PRIORITY_AT,
                correlation_id=DECISION_ID,
            )
        )
        self.lowered = write_activity(
            ActivityCommand(
                entity_type=ActivityRecord.EntityType.PROJECT,
                entity_id=PROJECT_CODE,
                verb=ActivityRecord.Verb.PRIORITY_CHANGED,
                origin=ActivityRecord.Origin.POLICY,
                actor=SYSTEM_ACTOR,
                occurred_at=PRIORITY_AT,
                correlation_id=DECISION_ID,
            )
        )

    def newest_first(self) -> tuple[int, ...]:
        """The ids the unfiltered feed must return, in the order it must return them."""
        return (
            self.lowered.id,
            self.raised.id,
            self.blocker_raised.id,
            self.task_added.id,
            self.moved.id,
            self.created.id,
        )
