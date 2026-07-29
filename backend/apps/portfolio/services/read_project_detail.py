"""Use case: read one project in full, including the moves it may legally make.

Served from the **write side**, not from ``ProjectSnapshot``. That is the deliberate opposite of
the queue: a list of 22 rows must not pay for a six-table join, while a single project must not be
rendered from a projection that a consumer has not rebuilt yet — an operator who has just moved a
project and lands on its detail page has to see the move.

``risk_flags`` and ``health`` arrive from ``read_project_priority``, which evaluates the
specifications against ``now`` rather than reading a stored set (ADR 0011). That is what makes this
response correct the moment it is asked for, including on the morning a target date passes with no
event having been emitted about it.

``transitions`` is the load-bearing part. It is the only source of transition buttons
(`docs/API.md` §2.2): the frontend holds no list of state codes and never guesses legality, which
is what makes adding a workflow state a fixture row and zero frontend changes.

``notes`` is served here rather than behind a ``/projects/{code}/notes`` route, the same way the
task detail serves its own: the panel that renders them is on this screen, and a second request is
a second chance to paint half a page.
"""

from datetime import datetime
from typing import Final

from apps.portfolio.domain.errors import ProjectNotFound
from apps.portfolio.domain.views import HealthRef, ProjectDetailView
from apps.portfolio.models import Project
from apps.prioritization.services.read_project_priority import read_project_priority
from apps.shared.refs import TaxonomyRef
from apps.work.models import Blocker, Note, Task
from apps.workflow.models import WorkflowTransition

#: How many of a project's comments the detail carries. A ceiling, not a page — the same contract
#: (and, deliberately, the same number) as ``TASK_NOTE_LIMIT``: the panel shows everything, so
#: paging would cost every reader a second request to learn there is no second page. Reaching it is
#: the signal to add a paginated ``/projects/{code}/notes`` *beside* this field, never instead of
#: it.
PROJECT_NOTE_LIMIT: Final = 100


def read_project_detail(*, project_code: str, now: datetime) -> ProjectDetailView:
    """Assemble the project detail response.

    Args:
        project_code: The business code (``PRJ-01``), never a primary key.
        now: Domain time. One instant for the whole response, so a task's ``is_overdue``, a
            blocker's ``age_days`` and an override's expiry are all judged against the same clock —
            a detail page whose parts disagreed about "now" is one nobody can reason about.

    Returns:
        The project with its tasks, blockers, score, computed risk flags, legal transitions and
        the newest :data:`PROJECT_NOTE_LIMIT` notes.

    Raises:
        ProjectNotFound: No project carries that code.
    """
    project = Project.objects.with_relations().by_code(project_code).first()
    if project is None:
        raise ProjectNotFound(project_code)

    today = now.date()
    # One scoped selection feeding both the counts and the rendered list, so the three numbers in
    # the header can never disagree with the rows underneath them: a removed task is absent from
    # ``open_tasks``, from ``overdue_tasks``, from ``blocked_tasks`` and from ``tasks`` at once
    # (ADR 0012). ``open_blockers`` is read from ``Blocker`` and is deliberately *not* scoped —
    # an impediment raised against a removed task is still an impediment on this project.
    tasks = Task.objects.active().for_project(project.pk)
    counts = tasks.counts(today=today)
    blockers = Blocker.objects.for_project(project.pk).with_relations().in_panel_order()
    priority = read_project_priority(project_code=project.code, now=now)

    return ProjectDetailView(
        code=project.code,
        name=project.name,
        summary=project.summary or None,
        # Long-form Markdown, delivered as the empty string rather than null: the editor mounts on
        # a document that is empty, not on one that is absent, and the column is NOT NULL.
        description=project.description,
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
        workflow=project.to_workflow_ref(),
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
        # ``for_project`` is the whole project's commentary, task-scoped notes included, and that
        # is a decision rather than an accident of the named query. Three things force it: the
        # write copies ``project`` onto a task note precisely so the project timeline finds it
        # without a join; the ``note.added`` envelope names the project for a task note too, so the
        # panel already renders one live and a project-only read would make it vanish on reload;
        # and ``NoteView`` carries ``task_code``, so a reader can still tell which piece of work a
        # comment is about. The narrower ``for_task`` exists for the opposite surface — the task
        # screen must not inherit the project's whole conversation — and is not the scope here.
        notes=Note.objects.for_project(project.pk).recent(PROJECT_NOTE_LIMIT).as_views(),
        updated_at=project.updated_at,
    )
