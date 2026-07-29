"""Use case: add a column to a lifecycle.

Two invariants live here and nowhere else.

**A new node appends.** ``order`` is the operator's arrangement, so a state created without one goes
after the last column of *this* graph rather than landing at ``0`` — the column model's default,
which would silently drop the new state in front of everything somebody already arranged and make
"add a state" a reshuffle. The number is read under the workflow's row lock, so two operators adding
a column at the same instant cannot both append at the same position.

**The first node of an empty graph is its entry node.** ``WorkflowState.entry_state`` refuses to
guess, so a graph with no ``is_initial`` state can hold no new project and no new task — a lifecycle
authored end to end through this API would otherwise be born unusable, and the operator would have
to visit the admin to fix a graph the product told them was complete. Every later state is an
ordinary node; the partial unique constraint is what keeps that irreversible-looking decision safe.
"""

from datetime import datetime
from uuid import UUID

from django.db import transaction

from apps.activity.models import ActivityRecord
from apps.activity.services import write_activity
from apps.workflow.domain.commands import AddStateCommand
from apps.workflow.domain.errors import DuplicateStateCode
from apps.workflow.domain.views import WorkflowShapeView
from apps.workflow.models import WorkflowState
from apps.workflow.services._authoring import (
    authoring_record,
    checked_category,
    locked_workflow,
    shape_of,
)


@transaction.atomic
def add_state(
    command: AddStateCommand,
    *,
    actor: str,
    correlation_id: UUID,
    now: datetime,
) -> WorkflowShapeView:
    """Add a node to a graph, appended unless the operator said where it goes.

    Args:
        command: Which graph, and the node to add.
        actor: ``accounts.User.code`` of the ops lead adding it.
        correlation_id: Threaded from the API boundary.
        now: Domain time, supplied by the caller.

    Returns:
        The whole graph as it now stands, so an editor can re-render the arrangement including the
        position it did not supply.

    Raises:
        WorkflowNotFound: ``command.workflow_code`` matches no graph.
        DuplicateStateCode: The graph already contains that code — including on a retired node,
            which is restored rather than duplicated, since the unique constraint is
            ``(workflow, code)`` and two rows would make the pair ambiguous.
        ValueOutsideVocabulary: ``category`` is outside the five the rest of the system branches on.
    """
    workflow = locked_workflow(command.workflow_code)
    category = checked_category(command.category)

    siblings = WorkflowState.objects.for_workflow(workflow.pk)
    if siblings.filter(code=command.code).exists():
        raise DuplicateStateCode(workflow.code, command.code)

    state = WorkflowState.objects.create(
        workflow=workflow,
        code=command.code,
        label=command.label,
        category=category,
        color=command.color,
        order=command.order if command.order is not None else siblings.next_order(),
        is_initial=not siblings.exists(),
    )

    write_activity(
        authoring_record(
            workflow_code=workflow.code,
            verb=ActivityRecord.Verb.STATE_ADDED,
            actor=actor,
            now=now,
            correlation_id=correlation_id,
            after=state.label,
            metadata={
                "state": state.code,
                "category": state.category,
                "order": state.order,
                "is_initial": state.is_initial,
            },
        )
    )
    return shape_of(workflow)
