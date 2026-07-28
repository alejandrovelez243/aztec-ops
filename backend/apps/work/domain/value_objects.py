"""Immutable values the ``work`` context passes across its own boundaries.

All of them are frozen Pydantic models (or a plain ``StrEnum``), validated at
construction rather than at the edge that consumes them, so a count that is negative or a
blocker kind that is not in the closed set cannot travel far enough to be persisted.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Final

from pydantic import BaseModel, ConfigDict, Field, JsonValue

#: Human labels for :class:`BlockerKind`, kept beside the enum rather than on the model so
#: ``domain/`` stays the single source of the vocabulary and ``models.py`` only renders it.
BLOCKER_KIND_LABELS: Final[dict[str, str]] = {
    "EXTERNAL_DEPENDENCY": "External dependency",
    "ACCESS": "Access",
    "DECISION": "Decision",
    "TECHNICAL": "Technical",
}


class BlockerKind(StrEnum):
    """What sort of impediment a blocker is.

    A closed, structural set — unlike a taxonomy in ``catalog``, an operator cannot add a
    member without a migration, because the risk and prioritization logic branches on
    these values. It lives in ``domain/`` instead of as a ``TextChoices`` on the model so
    that service commands can be typed against it without importing Django; ``models.py``
    renders it into the field's ``choices``.
    """

    EXTERNAL_DEPENDENCY = "EXTERNAL_DEPENDENCY"
    ACCESS = "ACCESS"
    DECISION = "DECISION"
    TECHNICAL = "TECHNICAL"

    @property
    def label(self) -> str:
        """The admin-facing name for this kind."""
        return BLOCKER_KIND_LABELS[self.value]


class ProjectTaskCounts(BaseModel):
    """The per-project task aggregates the prioritization engine and the snapshot need.

    Produced by one grouped query — ``Task.objects.for_project(...).counts()`` — rather than by
    four, because the four definitions must agree: "open" is ``WorkflowState.category NOT IN
    (DONE, CANCELLED)`` everywhere, and computing them apart is how one caller starts counting a
    cancelled task as open. That method is where the chain ends: it aggregates, so nothing can be
    narrowed after it.
    """

    model_config = ConfigDict(frozen=True)

    open_task_count: int = Field(ge=0)
    overdue_task_count: int = Field(ge=0)
    urgent_open_task_count: int = Field(ge=0)
    blocked_task_count: int = Field(ge=0)
    #: Tasks sitting in an ``IN_PROGRESS`` state. Counted here rather than probed with a separate
    #: ``exists()`` because "is anything moving on this project" is read by ``HasNoNextStep`` on
    #: every queue row, and a per-row probe over a page of projects is the N+1 the aggregate exists
    #: to prevent.
    in_progress_task_count: int = Field(default=0, ge=0)


class OpenBlockerSummary(BaseModel):
    """How many blockers are open on a project and how long the oldest has been open.

    ``oldest_raised_at`` is ``None`` exactly when ``open_blocker_count`` is zero. The age
    is not computed here: the ``blockage`` signal derives it from its own ``now``, so a
    replay of the same facts produces the same score.
    """

    model_config = ConfigDict(frozen=True)

    open_blocker_count: int = Field(ge=0)
    oldest_raised_at: datetime | None = None


class FieldChange(BaseModel):
    """One field that actually moved during an update, in both projections it is read in.

    ``before`` and ``after`` are already JSON-native — a referenced row rendered as its
    business ``code``, a date as an ISO string — because the same pair feeds an event
    payload, where a ``date`` or a ``Decimal`` would fail validation inside the producing
    transaction. ``None`` is kept as ``None`` rather than flattened to ``""``: "unassigned"
    and "assigned to nobody in particular" are the same fact only by accident.

    :attr:`from_value` / :attr:`to_value` are the audit projection, because
    ``ActivityRecord.from_value`` / ``to_value`` are non-null text columns holding a
    point-in-time copy that must stay readable after the row it described has changed again
    or been deleted. They are derived, not stored, so the audit trail and the event can never
    disagree about what moved.
    """

    model_config = ConfigDict(frozen=True)

    field: str
    before: JsonValue = None
    after: JsonValue = None

    @property
    def from_value(self) -> str:
        """The previous value as ``ActivityRecord`` text; ``""`` when there was none."""
        return "" if self.before is None else str(self.before)

    @property
    def to_value(self) -> str:
        """The new value as ``ActivityRecord`` text; ``""`` when the field was cleared."""
        return "" if self.after is None else str(self.after)

    def as_payload(self) -> dict[str, JsonValue]:
        """The pair as one entry of an ``*.updated`` event's ``changes`` map (EVENTS.md §4)."""
        return {"from": self.before, "to": self.after}
