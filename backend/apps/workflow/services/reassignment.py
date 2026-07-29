"""The reassignment rule: whether a record may be moved onto another lifecycle at all.

**The decision this module encodes, and why it is the one taken.** A record's legal moves are the
edges leaving the node it stands on. Point that record at a graph which does not contain that node
and it is parked outside its own lifecycle — no column to be drawn in, no move to make, and a
``workflow`` column naming a graph the ``workflow_state`` column contradicts. So the target graph
has to answer for the state the record is already on, and there are exactly two ways to make it:

1. **refuse**, naming the incompatibility, and make the operator resolve it first; or
2. **accept a landing state** from the caller and put the record there.

This module refuses. The second option reads as the friendlier one and is the wrong one here,
because it makes the reassignment route a second writer of ``workflow_state`` with no
``WorkflowTransition`` behind it — precisely what CLAUDE.md rule 2 forbids, and the one rule this
whole application exists to keep. "Reassign PRJ-01 to the short cycle, landing it on *entregado*"
is a state change wearing a configuration change's clothes: it would move a project to Done with no
edge traversed, no guard consulted, no required field checked and a trail entry saying the workflow
changed. A landing state also cannot be validated against anything — there is no edge from a node of
the old graph to a node of the new one, by construction, since an edge joins two states of one
graph — so the caller would be choosing freely from the target's states. That is an unrestricted
``UPDATE workflow_state``, exposed over HTTP, which is exactly the door rule 2 closes.

**So a reassignment never changes the state code.** It repoints the record at the equivalent node —
the state carrying the same ``code`` — in the target graph. The record keeps standing where it
stood; only the graph that owns the ground changes. That is what makes the operation safe to expose
without a transition behind it, and it is why the post-condition holds by construction: after a
successful reassignment the record sits on a state of its new workflow, and its legal moves come
from that workflow's edges.

**The failure mode is explicit.** No active state of the target carries the record's state code →
:class:`~apps.workflow.domain.errors.IncompatibleWorkflowState`, a 409 carrying the record, the
state it is standing on, both graphs and the states the target *does* offer. The operator then has
two honest ways forward, both of which keep every invariant: add the missing column to the target
graph (``POST /api/v1/workflows/{code}/states``), or move the record along its current graph — with
the transition service, obeying rule 2 — to a state the target does contain, and reassign then.

A retired state of the target is not a landing site either. Retirement means the node has left the
graph for new work, and ``retire_state`` refuses to strand records on one; arriving there through
the back door would create exactly what that refusal prevents.
"""

from typing import Protocol

from apps.workflow.domain.errors import (
    IncompatibleWorkflowState,
    WorkflowKindMismatch,
    WorkflowRetired,
)
from apps.workflow.domain.value_objects import ReassignmentCheck
from apps.workflow.models import Workflow, WorkflowState


class ReassignableEntity(Protocol):
    """The little a record has to expose to be moved between lifecycles.

    Structural, not a base class, exactly as
    :class:`~apps.workflow.services.transition.TransitionableEntity` is: ``portfolio.Project`` and
    ``work.Task`` satisfy it with the columns they already have, so the workflow context imports
    neither of them and neither inherits from anything here.
    """

    code: str
    workflow_state: WorkflowState


def validate_reassignment(
    *, entity: ReassignableEntity, target: Workflow, applies_to: str
) -> ReassignmentCheck:
    """Decide whether this record may follow ``target``, and where it would stand if it did.

    Performs no write and opens no transaction: the caller runs it inside the transaction where the
    result is applied, the same contract ``validate_transition`` has.

    Args:
        entity: The record being reassigned. Read-only here. Its ``workflow_state`` must be loaded
            with its ``workflow`` — chain the owning context's ``with_relations``.
        target: The graph it would follow. Already resolved by the caller through
            ``Workflow.objects.resolve``, so the precedence ladder has one home.
        applies_to: The kind of aggregate the caller owns — ``AppliesTo.PROJECT`` or
            ``AppliesTo.TASK``. Passed in because a structural protocol cannot state it and a graph
            governing the other kind would offer the record moves meant for something else.

    Returns:
        A :class:`~apps.workflow.domain.value_objects.ReassignmentCheck` naming both graphs, the
        state code that does not change, and the primary key of the node to land on.

    Raises:
        WorkflowKindMismatch: ``target`` governs the other kind of aggregate.
        WorkflowRetired: ``target`` is out of service and takes no arrivals.
        IncompatibleWorkflowState: ``target`` has no active state carrying the record's current
            state code. The whole failure mode of this operation — see the module docstring.
    """
    if target.applies_to != applies_to:
        raise WorkflowKindMismatch(target.code, target.applies_to, applies_to)

    current = entity.workflow_state
    source = current.workflow
    if not target.is_active and target.pk != source.pk:
        raise WorkflowRetired(target.code)

    landing = (
        WorkflowState.objects.for_workflow(target.pk).active().filter(code=current.code).first()
    )
    if landing is None:
        raise IncompatibleWorkflowState(
            entity.code,
            current.code,
            workflow_code=target.code,
            from_workflow_code=source.code,
            available=WorkflowState.objects.for_workflow(target.pk).active().codes(),
        )

    return ReassignmentCheck(
        entity_id=entity.code,
        from_workflow_code=source.code,
        from_workflow_name=source.name,
        to_workflow_code=target.code,
        to_workflow_name=target.name,
        state_code=current.code,
        landing_state_id=landing.pk,
    )
