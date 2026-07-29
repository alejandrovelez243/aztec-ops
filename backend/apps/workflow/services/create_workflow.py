"""Use case: add a lifecycle, so the operation can define one without a deploy.

The graph arrives empty and is shaped afterwards, one node and one edge at a time. That is not a
limitation of the payload: a lifecycle authored in a single request either succeeds whole or leaves
an operator re-typing a form, and every partial failure becomes a different rollback to reason
about. Adding the container first makes each later step independently retryable and independently
recorded in the trail.

No event is emitted. A brand new graph governs nothing — no project and no task resolves to it until
a binding sends work its way — so there is no score to recompute and no snapshot to rebuild. The
topic would be a broadcast no consumer could act on (CLAUDE.md rule 4 names the outbox as the
transport, not as a changelog).
"""

from datetime import datetime
from uuid import UUID

from django.db import transaction

from apps.activity.models import ActivityRecord
from apps.activity.services import write_activity
from apps.workflow.domain.commands import CreateWorkflowCommand
from apps.workflow.domain.errors import DuplicateWorkflowCode, EngagementTypeAlreadyBound
from apps.workflow.domain.views import WorkflowShapeView
from apps.workflow.models import Workflow, WorkflowBinding
from apps.workflow.services._authoring import (
    authoring_record,
    checked_applies_to,
    engagement_type_of,
    shape_of,
)


@transaction.atomic
def create_workflow(
    command: CreateWorkflowCommand,
    *,
    actor: str,
    correlation_id: UUID,
    now: datetime,
) -> WorkflowShapeView:
    """Create an empty, active graph and bind it to the engagement types it was named for.

    ``is_default`` is never set here. The fallback graph of an entity kind is what *unbound* work
    follows, and moving it silently while adding a lifecycle would reroute every project nobody
    bound — a decision that stays in the admin, behind a unique constraint.

    Args:
        command: The graph to add, and the engagement types to bind it to.
        actor: ``accounts.User.code`` of the ops lead creating it.
        correlation_id: Threaded from the API boundary; the creation and every binding it wrote
            share it.
        now: Domain time, supplied by the caller.

    Returns:
        The new graph as the document ``GET /api/v1/workflows`` publishes: no states, no
        transitions, and the engagement types now bound to it.

    Raises:
        DuplicateWorkflowCode: The code is taken — including by a retired graph, which is
            reactivated rather than duplicated.
        ValueOutsideVocabulary: ``applies_to`` is neither ``PROJECT`` nor ``TASK``.
        EngagementTypeNotFound: A named engagement type is not in the catalog.
        EngagementTypeAlreadyBound: A named engagement type already resolves to another graph for
            this entity kind, which the unique binding constraint forbids and resolution depends on.
    """
    applies_to = checked_applies_to(command.applies_to)
    if Workflow.objects.filter(code=command.code).exists():
        raise DuplicateWorkflowCode(command.code)

    workflow = Workflow.objects.create(
        code=command.code,
        name=command.name,
        applies_to=applies_to,
    )
    for engagement_type_code in command.engagement_types:
        _bind(workflow, engagement_type_code)

    write_activity(
        authoring_record(
            workflow_code=workflow.code,
            verb=ActivityRecord.Verb.CREATED,
            actor=actor,
            now=now,
            correlation_id=correlation_id,
            after=workflow.name,
            metadata={
                "applies_to": applies_to,
                "engagement_types": list(command.engagement_types),
            },
        )
    )
    return shape_of(workflow)


def _bind(workflow: Workflow, engagement_type_code: str) -> None:
    """Point one engagement type at this graph for the kind of aggregate the graph governs.

    Checked before inserting rather than caught afterwards: an ``IntegrityError` would abort the
    surrounding transaction and reach the client as a 500, while the conflict is a legitimate
    answer that names the graph already holding the binding.

    Raises:
        EngagementTypeAlreadyBound: Another graph already answers for this type and entity kind.
    """
    engagement_type = engagement_type_of(engagement_type_code)
    taken = (
        WorkflowBinding.objects.for_kind(workflow.applies_to)
        .filter(engagement_type=engagement_type)
        .select_related("workflow")
        .first()
    )
    if taken is not None:
        raise EngagementTypeAlreadyBound(
            engagement_type_code, taken.workflow.code, workflow.applies_to
        )
    WorkflowBinding.objects.create(
        workflow=workflow,
        applies_to=workflow.applies_to,
        engagement_type=engagement_type,
    )
