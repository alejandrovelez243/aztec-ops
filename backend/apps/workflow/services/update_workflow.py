"""Use case: rename a lifecycle, take it out of service, or put it back.

One module for the three because they are three halves of one decision — whether this lifecycle is
offered, and under what name — and because they share the rule that makes the surface safe to
expose: **absent means untouched, and only what actually moved is recorded**. Sending a graph its
current name and its current state is a successful no-op that writes nothing, because a trail that
records non-changes cannot be read for changes.

Retiring is ``is_active = False`` and never a delete. Projects and tasks ``PROTECT`` the states of
the graph they sit on, so the row could not be deleted even if the product wanted it to be; what
retirement means is that ``Workflow.objects.resolve`` stops handing this graph to new work while
everything already inside it keeps rendering, keeps transitioning and keeps being drawn.
"""

from datetime import datetime
from uuid import UUID

from django.db import transaction

from apps.activity.models import ActivityRecord
from apps.activity.services import write_activity
from apps.workflow.domain.commands import UpdateWorkflowCommand
from apps.workflow.domain.views import WorkflowShapeView
from apps.workflow.services._authoring import (
    FieldChange,
    authoring_record,
    locked_workflow,
    shape_of,
)

#: Which trail verb states each fact this use case can move. A rename and a retirement are separate
#: entries rather than one "workflow edited", because they are separate decisions with separate
#: readers: "who renamed this lifecycle" and "when did we stop offering it" are different questions,
#: unlike the four fields of a state, which one form moves at once.
_VERB_OF_FIELD = {
    "name": ActivityRecord.Verb.RENAMED,
    "activated": ActivityRecord.Verb.REACTIVATED,
    "deactivated": ActivityRecord.Verb.DEACTIVATED,
}


@transaction.atomic
def update_workflow(
    command: UpdateWorkflowCommand,
    *,
    actor: str,
    correlation_id: UUID,
    now: datetime,
) -> WorkflowShapeView:
    """Apply the fields that were sent and differ, recording one trail entry per fact that moved.

    Args:
        command: Which graph, and which fields to touch.
        actor: ``accounts.User.code`` of the ops lead editing it.
        correlation_id: Threaded from the API boundary; every entry this call writes shares it.
        now: Domain time, supplied by the caller.

    Returns:
        The graph as it now stands, in the shape ``GET /api/v1/workflows`` publishes.

    Raises:
        WorkflowNotFound: ``command.code`` matches no graph.
    """
    workflow = locked_workflow(command.code)
    sent = command.model_fields_set
    changed: list[FieldChange] = []

    if "name" in sent and command.name is not None and command.name != workflow.name:
        changed.append(FieldChange(field="name", before=workflow.name, after=command.name))
        workflow.name = command.name

    if (
        "is_active" in sent
        and command.is_active is not None
        and command.is_active != workflow.is_active
    ):
        workflow.is_active = command.is_active
        changed.append(
            FieldChange(
                field="activated" if workflow.is_active else "deactivated",
                before=str(not workflow.is_active).lower(),
                after=str(workflow.is_active).lower(),
            )
        )

    if not changed:
        return shape_of(workflow)

    workflow.save(update_fields=["name", "is_active"])
    for change in changed:
        write_activity(
            authoring_record(
                workflow_code=workflow.code,
                verb=_VERB_OF_FIELD[change.field],
                actor=actor,
                now=now,
                correlation_id=correlation_id,
                before=change.before,
                after=change.after,
            )
        )
    return shape_of(workflow)
