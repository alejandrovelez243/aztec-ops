"""Use case: read one project in full, including the moves it may legally make.

Served from the **write side**, not from ``ProjectSnapshot``. That is the deliberate opposite of
the queue: a list of 22 rows must not pay for a six-table join, while a single project must not be
rendered from a projection that a consumer has not rebuilt yet — an operator who has just moved a
project and lands on its detail page has to see the move.

``transitions`` is the load-bearing part. It is the only source of transition buttons
(`docs/API.md` §2.2): the frontend holds no list of state codes and never guesses legality, which
is what makes adding a workflow state a fixture row and zero frontend changes.
"""

from datetime import datetime

from apps.portfolio.domain.errors import ProjectNotFound
from apps.portfolio.domain.views import HealthRef, ProjectDetailView
from apps.portfolio.models import Project
from apps.prioritization.services.read_project_priority import read_project_priority
from apps.shared.refs import TaxonomyRef
from apps.work.models import Blocker, Task
from apps.workflow.models import WorkflowTransition


def read_project_detail(*, project_code: str, now: datetime) -> ProjectDetailView:
    """Assemble the project detail response.

    Args:
        project_code: The business code (``PRJ-01``), never a primary key.
        now: Domain time. One instant for the whole response, so a task's ``is_overdue``, a
            blocker's ``age_days`` and an override's expiry are all judged against the same clock —
            a detail page whose parts disagreed about "now" is one nobody can reason about.

    Returns:
        The project with its tasks, blockers, score, risk flags and legal transitions.

    Raises:
        ProjectNotFound: No project carries that code.
    """
    project = Project.objects.with_relations().by_code(project_code).first()
    if project is None:
        raise ProjectNotFound(project_code)

    today = now.date()
    tasks = Task.objects.for_project(project.pk)
    counts = tasks.counts(today=today)
    blockers = Blocker.objects.for_project(project.pk).with_relations().in_panel_order()
    priority = read_project_priority(project_id=project.pk, now=now)

    return ProjectDetailView(
        code=project.code,
        name=project.name,
        summary=project.summary or None,
        client=TaxonomyRef.of(code=project.client.code, label=project.client.alias),
        owner=project.owner.to_ref() if project.owner else None,
        engagement_type=TaxonomyRef.of(
            code=project.engagement_type.code,
            label=project.engagement_type.label,
            color=project.engagement_type.color,
        ),
        project_type=(
            TaxonomyRef.of(
                code=project.project_type.code,
                label=project.project_type.label,
                color=project.project_type.color,
            )
            if project.project_type
            else None
        ),
        stage=(
            TaxonomyRef.of(
                code=project.stage.code,
                label=project.stage.label,
                color=project.stage.color,
            )
            if project.stage
            else None
        ),
        state=project.workflow_state.to_ref(),
        health=HealthRef.of(priority.health.value),
        start_date=project.start_date,
        target_date=project.target_date,
        business_value=(
            float(project.business_value) if project.business_value is not None else None
        ),
        currency=project.currency.code,
        next_step=project.next_step or None,
        is_archived=project.is_archived,
        open_tasks=counts.open_task_count,
        overdue_tasks=counts.overdue_task_count,
        blocked_tasks=counts.blocked_task_count,
        open_blockers=Blocker.objects.for_project(project.pk).open().summary().open_blocker_count,
        score=priority.score,
        override=priority.override,
        risk_flags=priority.risk_flags,
        tasks=(
            tasks.with_relations().with_dependencies().in_attention_order().as_views(today=today)
        ),
        blockers=blockers.as_views(now=now),
        transitions=(
            WorkflowTransition.objects.active()
            .from_state(project.workflow_state_id)
            .with_states()
            .as_options()
        ),
        updated_at=project.updated_at,
    )
