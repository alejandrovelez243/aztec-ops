"""The transition rule: the only place that decides whether a state change is legal.

**What this service does and, deliberately, does not do.** It validates a proposed move against
the declared graph and returns what it validated. It does not assign `workflow_state`, does not
write the `ActivityRecord` and does not write the `OutboxEvent` — those belong to the calling
context's service, because the workflow context does not know which aggregate is moving. It has
no `Project` and no `Task` to name in `entity_type`, no idea which topic that aggregate publishes
under, and no business saying whether a task's move is worth an event of its own. Doing it here
would either hardcode `portfolio` into `workflow` or invent a generic event that no consumer can
interpret.

So the contract is a split, and both halves are mandatory:

1. this service validates and returns a `TransitionCheck`;
2. the owning service, inside one `transaction.atomic()`, assigns
   `entity.workflow_state_id = check.to_state_id`, writes the `ActivityRecord`
   (verb `STATE_CHANGED`, `from_value` / `to_value` from the check) and writes the `OutboxEvent`.

A caller that runs step 1 and skips step 2 has changed nothing, which is the safe direction. A
caller that assigns a state without step 1 is the bug this whole app exists to prevent
(CLAUDE.md rule 2).
"""

from collections.abc import Mapping
from datetime import datetime
from typing import Protocol

from apps.workflow.domain.errors import (
    GuardRejected,
    ReasonRequired,
    RequiredFieldMissing,
    TransitionNotAllowed,
)
from apps.workflow.domain.guards import resolve_guard
from apps.workflow.domain.value_objects import AttributeValue, TransitionCheck, TransitionSubject
from apps.workflow.models import WorkflowState, WorkflowTransition


class TransitionableEntity(Protocol):
    """The little a moving aggregate has to expose.

    Structural, not a base class: `portfolio.Project` and `work.Task` satisfy it by having the
    columns DATA_MODEL already gives them, so the workflow context depends on no other app's
    models and neither of them inherits from anything here.
    """

    code: str
    workflow_state_id: int
    workflow_state: WorkflowState


def validate_transition(
    *,
    entity: TransitionableEntity,
    to_state_code: str,
    actor: str,
    now: datetime,
    reason: str = "",
    guard_attributes: Mapping[str, AttributeValue] | None = None,
) -> TransitionCheck:
    """Validate a move against the declared graph and report what was checked.

    The target state is reachable only through an active `WorkflowTransition` leaving the
    aggregate's current state, matched inside that state's own workflow. An undeclared move — and
    an unknown state code, which is the same thing here — is `TransitionNotAllowed`, mapped to
    409, never a 500 and never a silent assignment.

    This function performs no write, so it needs no transaction of its own; the caller runs it
    inside the transaction where it will apply the result.

    Args:
        entity: The aggregate being moved. Read-only here.
        to_state_code: `WorkflowState.code` inside the aggregate's own workflow.
        actor: `accounts.User.code`, or `system` when the engine caused the change. Recorded on the
            result so the caller's `ActivityRecord` and the guard see the same actor.
        now: Domain time, passed in so a replay is deterministic and guards never read the clock.
        reason: Free text. Required when the transition sets `requires_reason`.
        guard_attributes: Facts the aggregate's own context supplies for the guard, e.g.
            `{"open_blocker_count": 2}`. Ignored when the transition names no guard.

    Returns:
        A `TransitionCheck` naming the edge, the target state id to assign and every rule that was
        evaluated.

    Raises:
        TransitionNotAllowed: No active edge from the current state to `to_state_code`.
        ReasonRequired: The edge sets `requires_reason` and `reason` is blank.
        RequiredFieldMissing: A field named in `requires_fields` is empty on the aggregate.
        GuardRejected: The registered guard refused, and carries the guard's own explanation.
        GuardNotRegistered: The edge names a guard no module registered.
    """
    from_state = entity.workflow_state
    transition = (
        WorkflowTransition.objects.active()
        .from_state(entity.workflow_state_id)
        .to_state_code(to_state_code)
        .with_states()
        .first()
    )
    if transition is None:
        raise TransitionNotAllowed(entity.code, from_state.code, to_state_code)

    if transition.requires_reason and not reason.strip():
        raise ReasonRequired(entity.code, from_state.code, to_state_code)

    checked_fields = _required_field_names(transition.requires_fields)
    _assert_fields_are_filled(entity=entity, field_names=checked_fields, to_state=to_state_code)

    to_state = transition.to_state
    guard_reason = _run_guard(
        guard=transition.guard,
        subject=TransitionSubject(
            entity_type=transition.workflow.applies_to,
            entity_id=entity.code,
            from_state_code=from_state.code,
            to_state_code=to_state.code,
            to_state_category=to_state.category,
            actor=actor,
            reason=reason,
            now=now,
            attributes=guard_attributes or {},
        ),
    )

    return TransitionCheck(
        transition_id=transition.pk,
        entity_type=transition.workflow.applies_to,
        entity_id=entity.code,
        from_state_id=from_state.pk,
        from_state_code=from_state.code,
        from_state_category=from_state.category,
        to_state_id=to_state.pk,
        to_state_code=to_state.code,
        to_state_category=to_state.category,
        label=transition.label,
        actor=actor,
        reason=reason,
        reason_was_required=transition.requires_reason,
        checked_fields=checked_fields,
        guard=transition.guard,
        guard_reason=guard_reason,
        checked_at=now,
    )


def _required_field_names(requires_fields: object) -> tuple[str, ...]:
    """Read the JSONB list defensively: an operator edits it as free-form JSON in the admin."""
    if not isinstance(requires_fields, list):
        return ()
    return tuple(str(name) for name in requires_fields if str(name).strip())


def _assert_fields_are_filled(
    *, entity: TransitionableEntity, field_names: tuple[str, ...], to_state: str
) -> None:
    """Raise on the first empty required field, naming it so the UI can point at one input."""
    for field_name in field_names:
        if _is_empty(getattr(entity, field_name, None)):
            raise RequiredFieldMissing(entity.code, field_name, to_state)


def _is_empty(value: object) -> bool:
    """Empty means null, blank text or an empty collection — `0` and `False` are real values."""
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, list | tuple | set | dict):
        return not value
    return False


def _run_guard(*, guard: str, subject: TransitionSubject) -> str:
    """Resolve and run the named guard, translating its refusal into the typed error."""
    if not guard:
        return ""
    outcome = resolve_guard(guard)(subject)
    if not outcome.allowed:
        raise GuardRejected(guard, subject.entity_id, outcome.reason)
    return outcome.reason
