"""Use case: take a column out of a lifecycle — the one write that can refuse for someone else.

**Nothing is ever deleted.** ``WorkflowState`` is ``PROTECT``ed by every project and task pointing
at it, so a delete would surface as a database error naming a constraint, which is not an answer an
operator can act on. Retirement is ``is_active = False``, and the ``DELETE`` route means exactly
that: take this node out of the graph, keep the row that history depends on.

**And it is refused while records occupy it.** A retirement that went through would leave those
records parked on a node the graph no longer contains: no column to draw them in, and their next
move decided by arrows that are no longer part of the lifecycle. So the count is read first and the
refusal carries it — :class:`~apps.workflow.domain.errors.WorkflowStateInUse` reports how many
projects and how many tasks, because "that failed" is not something an operator can act on either.

**Retiring a node withdraws its arrows.** Every edge touching it — leaving or landing — is
deactivated in the same transaction. An arrow into a node outside the graph is a move nothing may
take, and an arrow out of it is a move nothing can be standing on to take; leaving either behind
would publish a diagram that contradicts its own column set. They are recorded as part of this one
decision rather than as separate withdrawals, which is what ``metadata.withdrawn_transitions`` is.

The mirror image is deliberately *allowed*: retiring the last move **out of** a state is how a
terminal state is declared, and nothing here refuses it — that path is
:mod:`~apps.workflow.services.retire_transition`, which touches no node at all.
"""

from datetime import datetime
from uuid import UUID

from django.db import transaction
from pydantic import JsonValue

from apps.activity.models import ActivityRecord
from apps.activity.services import write_activity
from apps.workflow.domain.errors import WorkflowStateInUse
from apps.workflow.domain.value_objects import StateOccupancy
from apps.workflow.domain.views import WorkflowShapeView
from apps.workflow.models import WorkflowTransition
from apps.workflow.repositories import records_on_states
from apps.workflow.services._authoring import (
    authoring_record,
    locked_workflow,
    shape_of,
    state_of,
)


@transaction.atomic
def retire_state(
    *,
    workflow_code: str,
    state_code: str,
    actor: str,
    correlation_id: UUID,
    now: datetime,
) -> WorkflowShapeView:
    """Take a node out of a graph, together with every edge that touches it.

    Retiring an already retired node is a successful no-op that writes nothing: the operator asked
    for a state of the world that already holds, and a trail entry claiming a change would be false.

    If the node was the graph's entry node, the graph can no longer place new aggregates —
    ``WorkflowState.objects.entry_state`` selects the *active* initial node and raises
    ``WorkflowNotConfigured`` when there is none. That is deliberate and loud: silently starting new
    work on a column the operator removed is the failure this refuses to hide.

    Args:
        workflow_code: The graph the node belongs to.
        state_code: The node to retire.
        actor: ``accounts.User.code`` of the ops lead retiring it.
        correlation_id: Threaded from the API boundary; the retirement and the arrows it withdrew
            share it, so the whole decision reads back as one.
        now: Domain time, supplied by the caller.

    Returns:
        The whole graph as it now stands, with the node flagged ``is_active: false`` and the
        withdrawn arrows already gone from ``transitions``.

    Raises:
        WorkflowNotFound: ``workflow_code`` matches no graph.
        WorkflowStateNotFound: The graph has no node with that code.
        WorkflowStateInUse: Projects or tasks are sitting on the node. The error names how many of
            each, which is what an operator needs before they can move them.
    """
    workflow = locked_workflow(workflow_code)
    state = state_of(workflow, state_code)

    occupancy = records_on_states([state.pk]).get(state.pk, StateOccupancy())
    if occupancy.total:
        raise WorkflowStateInUse(
            workflow.code,
            state.code,
            projects=occupancy.projects,
            tasks=occupancy.tasks,
        )

    if not state.is_active:
        return shape_of(workflow)

    withdrawn = _withdraw_edges_touching(workflow_id=workflow.pk, state_id=state.pk)
    state.is_active = False
    state.save(update_fields=["is_active"])

    write_activity(
        authoring_record(
            workflow_code=workflow.code,
            verb=ActivityRecord.Verb.STATE_RETIRED,
            actor=actor,
            now=now,
            correlation_id=correlation_id,
            before="active",
            after="retired",
            metadata={"state": state.code, "withdrawn_transitions": withdrawn},
        )
    )
    return shape_of(workflow)


def _withdraw_edges_touching(*, workflow_id: int, state_id: int) -> list[JsonValue]:
    """Deactivate every active edge with this node at either end, and name them for the trail.

    Read before the update rather than after: once ``is_active`` is false the set is no longer
    selectable by the same query, and a trail entry that could not say which moves disappeared would
    make the retirement unreviewable.

    Args:
        workflow_id: The graph being edited.
        state_id: The node being retired.

    Returns:
        One ``{"from": code, "to": code}`` per withdrawn edge, in graph order.
    """
    edges = (
        WorkflowTransition.objects.for_workflow(workflow_id)
        .touching(state_id)
        .active()
        .with_states()
        .in_graph_order()
    )
    withdrawn: list[JsonValue] = [
        {"from": edge.from_state.code, "to": edge.to_state.code} for edge in edges
    ]
    WorkflowTransition.objects.for_workflow(workflow_id).touching(state_id).active().update(
        is_active=False
    )
    return withdrawn
