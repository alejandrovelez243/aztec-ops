"""Reference guard: an aggregate may not be parked in a blocked state with nothing blocking it.

Attached from the admin to every edge whose target category is `BLOCKED`, by typing
`require_open_blocker` into `WorkflowTransition.guard`. It exists because "blocked" is the single
most consequential label in this system — it drives the `BLOCKED` risk flag, the `blockage`
priority signal and the blockers panel — and a board where "blocked" sometimes means "nobody
wrote down why" stops being usable for running the operation. Requiring the `Blocker` row first
makes the impediment a first-class fact with an owner and an age (ARCHITECTURE §3.3) instead of a
sentence in a notes field.
"""

from apps.workflow.domain.guards.registry import register_guard
from apps.workflow.domain.value_objects import GuardOutcome, TransitionSubject

#: The fact this guard needs from the calling context, counted over unresolved blockers only.
OPEN_BLOCKER_COUNT: str = "open_blocker_count"


@register_guard("require_open_blocker")
def require_open_blocker(subject: TransitionSubject) -> GuardOutcome:
    """Allow the move only when the aggregate already has at least one open blocker.

    A missing or non-integer `open_blocker_count` is a rejection, not a pass: the fact is supplied
    by the calling service, so its absence is a programming error, and defaulting to "allow" would
    turn a forgotten argument into a silently disabled rule.

    Args:
        subject: The proposed move, with `attributes["open_blocker_count"]` set by the caller.

    Returns:
        Allowed when the count is positive; otherwise a rejection naming what is missing.
    """
    count = subject.attributes.get(OPEN_BLOCKER_COUNT)
    if not isinstance(count, int) or isinstance(count, bool):
        return GuardOutcome(
            allowed=False,
            reason=(
                f"The open blocker count of {subject.entity_id} was not supplied, so the move to "
                f"'{subject.to_state_code}' cannot be justified."
            ),
        )
    if count <= 0:
        return GuardOutcome(
            allowed=False,
            reason=(
                f"{subject.entity_id} has no open blocker; raise the impediment first so it has "
                f"an owner and an age, then move it to '{subject.to_state_code}'."
            ),
        )
    return GuardOutcome(
        allowed=True,
        reason=f"{count} open blocker(s) justify moving to '{subject.to_state_code}'.",
    )
