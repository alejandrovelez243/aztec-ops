"""Use case: withdraw a declared move.

**Nothing is deleted here either**, and the reason is different from the one that protects a node.
A state is protected by the records standing on it; an edge is protected by history — the trail says
"``ejecucion -> bloqueado``" and the events say the same, so a row deleted out from under those
sentences turns a readable audit into two codes nothing resolves. ``is_active = False`` keeps the
row, and the transition service stops offering the move on its next read, which is the same instant.

**Withdrawing the last move out of a state is allowed, and that is the point.** It is how a terminal
state is declared: nothing leaves ``entregado``, so ``entregado`` is where a lifecycle ends. Refusing
it would make "declare an end state" impossible to express through the product, and would be an
invented rule — no record can be harmed by losing a move it had not taken, since a record sitting on
that state simply has no buttons, which is exactly what a terminal state means.

The asymmetry with :mod:`~apps.workflow.services.retire_state` is therefore deliberate, not an
oversight: retiring a *node* is refused while records occupy it, because those records would be left
outside the graph; withdrawing an *edge* leaves every record exactly where it is.
"""

from datetime import datetime
from uuid import UUID

from django.db import transaction

from apps.activity.models import ActivityRecord
from apps.activity.services import write_activity
from apps.workflow.domain.views import WorkflowShapeView
from apps.workflow.services._authoring import (
    authoring_record,
    locked_workflow,
    shape_of,
    transition_of,
)


@transaction.atomic
def retire_transition(
    *,
    workflow_code: str,
    from_state: str,
    to_state: str,
    actor: str,
    correlation_id: UUID,
    now: datetime,
) -> WorkflowShapeView:
    """Withdraw one edge, leaving its row and both of its endpoints untouched.

    Withdrawing an already withdrawn move is a successful no-op that writes nothing: the operator
    asked for a state of the world that already holds.

    Args:
        workflow_code: The graph the edge belongs to.
        from_state: Source node code.
        to_state: Target node code.
        actor: ``accounts.User.code`` of the ops lead withdrawing it.
        correlation_id: Threaded from the API boundary.
        now: Domain time, supplied by the caller.

    Returns:
        The whole graph as it now stands, with the arrow already absent from ``transitions`` — a
        withdrawn edge is not published as a drawable arrow, because nothing can take it.

    Raises:
        WorkflowNotFound: ``workflow_code`` matches no graph.
        TransitionNotFound: The graph declares no move between those two nodes.
    """
    workflow = locked_workflow(workflow_code)
    transition = transition_of(workflow, from_state, to_state)

    if not transition.is_active:
        return shape_of(workflow)

    transition.is_active = False
    transition.save(update_fields=["is_active"])

    write_activity(
        authoring_record(
            workflow_code=workflow.code,
            verb=ActivityRecord.Verb.TRANSITION_RETIRED,
            actor=actor,
            now=now,
            correlation_id=correlation_id,
            before="active",
            after="retired",
            metadata={
                "from_state": transition.from_state.code,
                "to_state": transition.to_state.code,
                "label": transition.label,
            },
        )
    )
    return shape_of(workflow)
