"""Use case: change what a declared move asks for, or put a withdrawn one back.

**Absent means untouched, and only what moved is written**, exactly as for a node. The ordered pair
of endpoints is the address and is never a field: an edge *is* its endpoints, so "repoint this
arrow" is withdrawing one move and declaring another — two decisions, two lines in the trail, and
two chances for an operator to notice that the second one is not what they meant.

``requires_fields`` is sent whole. A list edited by patching one entry has no unambiguous spelling
on the wire, and the field is short by nature: it names aggregate attributes an operator must fill
before the move, not a collection that grows.
"""

from datetime import datetime
from uuid import UUID

from django.db import transaction

from apps.activity.models import ActivityRecord
from apps.activity.services import write_activity
from apps.workflow.domain.commands import UpdateTransitionCommand
from apps.workflow.domain.views import WorkflowShapeView, required_field_names
from apps.workflow.services._authoring import (
    FieldChange,
    authoring_record,
    changed_fields,
    checked_guard,
    locked_workflow,
    renaming,
    shape_of,
    transition_of,
)

#: The columns this use case may write. Listed once so ``update_fields`` cannot drift from the
#: branches above it — a field assigned and left out of the list is a change that silently vanishes.
_EDITABLE_FIELDS = ["label", "requires_reason", "requires_fields", "guard", "order", "is_active"]


@transaction.atomic
def update_transition(
    command: UpdateTransitionCommand,
    *,
    actor: str,
    correlation_id: UUID,
    now: datetime,
) -> WorkflowShapeView:
    """Apply the edge fields that were sent and differ, and record the edit once.

    A withdrawn edge is editable, and ``is_active: true`` is how it comes back — on this same row,
    never a second one for the same pair, which the unique constraint forbids and which would leave
    the transition service with two rows to choose between. The field is typed ``Literal[True]``:
    withdrawing a move is ``DELETE``, so this surface cannot express it.

    Args:
        command: Which graph, which edge, and which fields to touch.
        actor: ``accounts.User.code`` of the ops lead editing it.
        correlation_id: Threaded from the API boundary.
        now: Domain time, supplied by the caller.

    Returns:
        The whole graph as it now stands.

    Raises:
        WorkflowNotFound: ``command.workflow_code`` matches no graph.
        TransitionNotFound: The graph declares no move between those two nodes.
        GuardNotRegistered: ``guard`` names a callable nothing registered.
    """
    workflow = locked_workflow(command.workflow_code)
    transition = transition_of(workflow, command.from_state, command.to_state)
    sent = command.model_fields_set
    changed: list[FieldChange] = []

    if "label" in sent and command.label is not None and command.label != transition.label:
        changed.append(FieldChange(field="label", before=transition.label, after=command.label))
        transition.label = command.label

    if (
        "requires_reason" in sent
        and command.requires_reason is not None
        and command.requires_reason != transition.requires_reason
    ):
        changed.append(
            FieldChange(
                field="requires_reason",
                before=str(transition.requires_reason).lower(),
                after=str(command.requires_reason).lower(),
            )
        )
        transition.requires_reason = command.requires_reason

    if "requires_fields" in sent and command.requires_fields is not None:
        current = required_field_names(transition.requires_fields)
        proposed = required_field_names(list(command.requires_fields))
        if proposed != current:
            changed.append(
                FieldChange(
                    field="requires_fields",
                    before=", ".join(current),
                    after=", ".join(proposed),
                )
            )
            transition.requires_fields = list(proposed)

    if "guard" in sent and command.guard is not None:
        guard = checked_guard(command.guard)
        if guard != transition.guard:
            changed.append(FieldChange(field="guard", before=transition.guard, after=guard))
            transition.guard = guard

    if "order" in sent and command.order is not None and command.order != transition.order:
        changed.append(
            FieldChange(field="order", before=str(transition.order), after=str(command.order))
        )
        transition.order = command.order

    # Restore only, and the only way back: a second row for the same ordered pair is forbidden by
    # constraint, so ``add_transition`` answers ``DuplicateTransition`` for a withdrawn edge.
    if "is_active" in sent and command.is_active is True and not transition.is_active:
        changed.append(FieldChange(field="is_active", before="false", after="true"))
        transition.is_active = True

    if not changed:
        return shape_of(workflow)

    transition.save(update_fields=_EDITABLE_FIELDS)
    rename = renaming(changed)
    write_activity(
        authoring_record(
            workflow_code=workflow.code,
            verb=ActivityRecord.Verb.TRANSITION_EDITED,
            actor=actor,
            now=now,
            correlation_id=correlation_id,
            before=rename.before if rename is not None else "",
            after=rename.after if rename is not None else "",
            metadata={
                "from_state": transition.from_state.code,
                "to_state": transition.to_state.code,
                "changed": changed_fields(changed),
            },
        )
    )
    return shape_of(workflow)
