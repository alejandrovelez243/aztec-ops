"""Use case: declare a move between two columns of the same lifecycle.

**Coherence is enforced here, not hoped for.** Both endpoints are resolved *inside* the graph named
in the command, so an edge joining two lifecycles cannot be expressed: a state code is unique only
inside its workflow, and a global lookup would let ``bloqueada`` from another graph become the
target of this one — putting an aggregate into a state its own lifecycle does not contain. The
model's ``clean()`` says the same thing to the admin; this says it to the API, and neither delegates
to the other.

**A guard is checked at authoring time.** ``WorkflowTransition.guard`` names a registered callable,
and a typo would otherwise sit in the graph claiming to enforce a safety check that evaporated —
the transition service raises ``GuardNotRegistered`` when a *record* tries to move, which is a
rejection aimed at the wrong person, hours or weeks after the mistake. Adding a guard is still one
function plus one registry line (CLAUDE.md rule 8); this only refuses codes nothing answers to.

Declaring an edge changes nothing about any record. The move becomes *available* from the state it
leaves, and whether a given project or task may take it is still decided per record by
``validate_transition``, against the row it is sitting on, the fields filled in on it and the guard.
"""

from datetime import datetime
from uuid import UUID

from django.db import transaction

from apps.activity.models import ActivityRecord
from apps.activity.services import write_activity
from apps.workflow.domain.commands import AddTransitionCommand
from apps.workflow.domain.errors import DuplicateTransition
from apps.workflow.domain.views import WorkflowShapeView, required_field_names
from apps.workflow.models import WorkflowTransition
from apps.workflow.services._authoring import (
    authoring_record,
    checked_guard,
    locked_workflow,
    shape_of,
    state_of,
)


@transaction.atomic
def add_transition(
    command: AddTransitionCommand,
    *,
    actor: str,
    correlation_id: UUID,
    now: datetime,
) -> WorkflowShapeView:
    """Declare an edge, appended to the moves leaving its source unless told where it goes.

    Args:
        command: Which graph, which two nodes, and what the move asks for.
        actor: ``accounts.User.code`` of the ops lead declaring it.
        correlation_id: Threaded from the API boundary.
        now: Domain time, supplied by the caller.

    Returns:
        The whole graph as it now stands, so an editor re-renders the arrows in the operator's
        arrangement including the position it did not supply.

    Raises:
        WorkflowNotFound: ``command.workflow_code`` matches no graph.
        WorkflowStateNotFound: One of the endpoints is not a node of this graph — which is also the
            answer when it names a node of a different one.
        DuplicateTransition: This ordered pair already has an edge. Re-enabling a withdrawn move is
            ``PATCH``/restore on the existing row, never a second one that would make the graph hold
            two rows for one move.
        GuardNotRegistered: ``guard`` names a callable nothing registered.
    """
    workflow = locked_workflow(command.workflow_code)
    from_state = state_of(workflow, command.from_state)
    to_state = state_of(workflow, command.to_state)
    guard = checked_guard(command.guard)

    siblings = WorkflowTransition.objects.for_workflow(workflow.pk).from_state(from_state.pk)
    if siblings.filter(to_state=to_state).exists():
        raise DuplicateTransition(workflow.code, from_state.code, to_state.code)

    transition = WorkflowTransition.objects.create(
        workflow=workflow,
        from_state=from_state,
        to_state=to_state,
        label=command.label,
        requires_reason=command.requires_reason,
        requires_fields=list(required_field_names(list(command.requires_fields))),
        guard=guard,
        order=command.order if command.order is not None else siblings.next_order(),
    )

    write_activity(
        authoring_record(
            workflow_code=workflow.code,
            verb=ActivityRecord.Verb.TRANSITION_ADDED,
            actor=actor,
            now=now,
            correlation_id=correlation_id,
            after=f"{from_state.code} -> {to_state.code}",
            metadata={
                "from_state": from_state.code,
                "to_state": to_state.code,
                "label": transition.label,
                "requires_reason": transition.requires_reason,
                "requires_fields": list(required_field_names(transition.requires_fields)),
                "guard": guard,
                "order": transition.order,
            },
        )
    )
    return shape_of(workflow)
