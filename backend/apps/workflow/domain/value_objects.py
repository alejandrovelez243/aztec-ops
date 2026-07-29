"""Immutable values that cross the workflow context's boundaries.

Frozen Pydantic models, compared by value, validated at construction. They are what lets a guard
be written and tested without a database: a guard sees a `TransitionSubject`, never an ORM
instance it could query through.
"""

from collections.abc import Mapping
from datetime import datetime

from pydantic import BaseModel, ConfigDict

#: What an aggregate is allowed to expose to a guard. Deliberately narrow: dates and decimals are
#: passed as strings or day counts by the calling context, so a guard can never reach back into
#: the ORM through an attribute it was handed.
AttributeValue = str | int | float | bool | None


class TransitionSubject(BaseModel):
    """The facts a guard may see about the aggregate being moved.

    The workflow context does not know what a project or a task is, so the calling context
    supplies the facts a guard needs in `attributes` (for example `open_blocker_count`). A missing
    fact is a programming error in the calling service, not an operator action, so a guard rejects
    the move and names what it did not receive rather than defaulting to "allow" — a forgotten
    argument must not silently disable a rule an edge claims to enforce.
    """

    model_config = ConfigDict(frozen=True)

    entity_type: str
    entity_id: str
    from_state_code: str
    to_state_code: str
    to_state_category: str
    actor: str
    reason: str
    now: datetime
    attributes: Mapping[str, AttributeValue] = {}


class GuardOutcome(BaseModel):
    """A guard's verdict plus the sentence that justifies it.

    The reason is mandatory on a rejection and is rendered to the operator verbatim, which is why
    a guard returns this instead of a bare bool: "not allowed" with no fact behind it is a dead
    end for whoever has to unblock the work.
    """

    model_config = ConfigDict(frozen=True)

    allowed: bool
    reason: str = ""


class StateOccupancy(BaseModel):
    """How many records are standing on one state, split by the context that owns them.

    The answer :func:`~apps.workflow.repositories.records_on_states` gives, and the reason
    retirement can refuse with a sentence an operator can act on: "3 proyectos y 1 tarea" says where
    to go and look, while a single total says only that the button did not work.

    Split rather than summed because the two halves are moved by different people through different
    screens. A pure value: it holds counts, never rows, so nothing that receives one can reach back
    into ``portfolio`` or ``work`` through it.
    """

    model_config = ConfigDict(frozen=True)

    projects: int = 0
    tasks: int = 0

    @property
    def total(self) -> int:
        """Every record blocking the retirement, which is what the graph document publishes."""
        return self.projects + self.tasks


class TransitionCheck(BaseModel):
    """What the transition service validated, and the state the caller may now assign.

    Returned instead of a mutated aggregate on purpose (see `services/transition.py`): the
    workflow context does not know which aggregate is moving, so it validates and reports, and
    the owning context performs the write, the `ActivityRecord` and the `OutboxEvent` in its own
    transaction. `to_state_id` is the only thing a caller needs for the assignment
    (`entity.workflow_state_id = check.to_state_id`), which keeps this a plain value with no ORM
    instance inside it.
    """

    model_config = ConfigDict(frozen=True)

    transition_id: int
    entity_type: str
    entity_id: str
    from_state_id: int
    from_state_code: str
    from_state_category: str
    to_state_id: int
    to_state_code: str
    to_state_category: str
    label: str
    actor: str
    reason: str
    reason_was_required: bool
    checked_fields: tuple[str, ...]
    guard: str
    guard_reason: str
    checked_at: datetime

    @property
    def edge_code(self) -> str:
        """Stable name of the edge that was traversed, for the ``transition`` event field.

        Derived rather than stored: ``WorkflowTransition`` has no ``code`` column (DATA_MODEL §2),
        its identity being the ``(workflow, from_state, to_state)`` triple. Rendering the pair as
        ``execution__blocked`` gives consumers and the audit trail a name that survives a primary
        key change and reads the same in `docs/EVENTS.md` §4 as it does in the payload.
        """
        return f"{self.from_state_code}__{self.to_state_code}"
