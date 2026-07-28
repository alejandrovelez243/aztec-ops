"""Use case: register a new project in the portfolio."""

from datetime import datetime
from uuid import UUID

from django.db import transaction

from apps.activity.domain.value_objects import ActivityCommand
from apps.activity.services import write_activity
from apps.events.domain.envelope import ENTITY_PROJECT, TOPIC_PROJECT_CREATED
from apps.events.services import enqueue_event
from apps.portfolio.domain.errors import DuplicateProjectCode
from apps.portfolio.domain.rules import (
    ensure_non_negative_business_value,
    ensure_valid_date_window,
)
from apps.portfolio.domain.value_objects import CreateProjectCommand, ProjectResult
from apps.portfolio.models import Project
from apps.portfolio.repositories import ProjectRepository
from apps.portfolio.services._references import (
    resolve_client,
    resolve_engagement_type,
    resolve_owner,
    resolve_project_type,
    resolve_stage,
)
from apps.workflow import repositories as workflow_repositories
from apps.workflow.models import AppliesTo


@transaction.atomic
def create_project(
    command: CreateProjectCommand,
    *,
    actor: str,
    correlation_id: UUID,
    now: datetime,
) -> ProjectResult:
    """Create a project in the initial state of the workflow its engagement type binds to.

    The caller never chooses the starting state. It is resolved from ``WorkflowBinding`` — binding
    for this engagement type, then the binding whose engagement type is null, then the default
    workflow — so a Diagnostico can start a shorter lifecycle than a recurring engagement without a
    code change (DATA_MODEL §2).

    The row, the ``ActivityRecord`` and the ``OutboxEvent`` are written in one transaction, so a
    caller either sees all three or none. Nothing is published to Redis here: the relay publishes
    the outbox row after commit, which is what makes the event as atomic as the project itself.
    Scoring and risk evaluation are not done here either — they are consumers reacting to
    ``project.created``, so a failing recalculation cannot roll back a legitimate creation.

    Args:
        command: The requested project, addressed by business codes.
        actor: ``accounts.User.code``, or "system" when the engine caused the change.
        correlation_id: Threaded from the API boundary so related changes reconstruct as one
            movement.
        now: Domain time. A parameter, never ``timezone.now()`` inside the body, so a replay is
            deterministic and the service is testable.

    Returns:
        The created project as a frozen projection.

    Raises:
        DuplicateProjectCode: The business code is already taken.
        WorkflowNotConfigured: No workflow binding or default answers for projects, or the
            resolved workflow has no entry node.
        ClientNotFound: ``client_code`` matches no client.
        TaxonomyEntryNotFound: An engagement type, project type or stage code matches no row.
        OwnerNotFound: ``owner_code`` matches nobody.
        InvalidDateWindow: ``start_date`` is after ``target_date``.
        NegativeBusinessValue: ``business_value`` is below zero.
    """
    repository = ProjectRepository()
    if repository.code_exists(command.code):
        raise DuplicateProjectCode(command.code)

    ensure_valid_date_window(command.start_date, command.target_date)
    ensure_non_negative_business_value(command.business_value)

    engagement_type = resolve_engagement_type(command.engagement_type_code)
    workflow = workflow_repositories.resolve_workflow(
        applies_to=AppliesTo.PROJECT,
        engagement_type_id=engagement_type.pk,
    )
    initial_state = workflow_repositories.initial_state(workflow_id=workflow.pk)

    project = Project.objects.create(
        code=command.code,
        name=command.name,
        client=resolve_client(command.client_code),
        engagement_type=engagement_type,
        project_type=resolve_project_type(command.project_type_code),
        stage=resolve_stage(command.stage_code),
        workflow_state=initial_state,
        owner=resolve_owner(command.owner_code),
        start_date=command.start_date,
        target_date=command.target_date,
        business_value=command.business_value,
        currency=command.currency,
        summary=command.summary,
        next_step=command.next_step,
        imported_health=command.imported_health,
    )

    write_activity(
        ActivityCommand(
            entity_type=ENTITY_PROJECT,
            entity_id=project.code,
            verb="CREATED",
            actor=actor,
            to_value=initial_state.code,
            metadata={
                "engagement_type": engagement_type.code,
                "owner": command.owner_code or "",
                "has_target_date": command.target_date is not None,
            },
            occurred_at=now,
            correlation_id=correlation_id,
        )
    )
    enqueue_event(
        topic=TOPIC_PROJECT_CREATED,
        entity_type=ENTITY_PROJECT,
        entity_id=project.code,
        payload={
            "name": project.name,
            "client_alias": project.client.alias,
            "engagement_type": engagement_type.code,
            "project_type": project.project_type.code if project.project_type else None,
            "stage": project.stage.code if project.stage else None,
            "state": initial_state.code,
            "owner_alias": project.owner.alias if project.owner else None,
            "target_date": command.target_date.isoformat() if command.target_date else None,
            "business_value": (
                float(project.business_value) if project.business_value is not None else None
            ),
            "currency": project.currency,
        },
        actor=actor,
        correlation_id=correlation_id,
        occurred_at=now,
    )

    return project.to_result()
