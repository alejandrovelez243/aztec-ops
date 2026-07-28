"""Every query the ``work`` context makes, and nowhere else.

Two kinds of query live here. The first are ``work``'s own reads, where the filter *is* a
business definition — "open" is ``WorkflowState.category NOT IN (DONE, CANCELLED)`` and
"open blocker" is ``resolved_at IS NULL``, and those definitions drift the moment they are
written twice. The second are the lookups ``work`` makes into the published aggregates of
``portfolio`` and ``catalog``; they are collected here so the coupling is one readable file
rather than an import scattered through six services.

Resolving a person by code is deliberately *not* one of them any more. It used to be
``team_member_by_code`` here and ``TeamMemberRepository.get_by_code`` in ``portfolio``, the same
definition written twice; it now has one home in ``apps.accounts.repositories.user_by_code``,
which services import directly.

Repositories return materialized lists and frozen value objects, never a ``QuerySet`` the
caller can extend, because a lazily-extended queryset moves the query back out of this file.
"""

from __future__ import annotations

from collections import defaultdict
from typing import TYPE_CHECKING

from django.db.models import Count, Min, Q

from apps.catalog.models import Priority
from apps.portfolio.models import Project
from apps.work.domain.value_objects import OpenBlockerSummary, ProjectTaskCounts
from apps.work.models import Blocker, Note, Task, TaskDependency
from apps.workflow.models import StateCategory

if TYPE_CHECKING:
    from datetime import date


class TaskRepository:
    """Reads over ``work_task`` and its dependency edges."""

    def get_by_code(self, task_code: str) -> Task | None:
        """Return the task with this business code, or ``None``.

        Joins the rows every caller immediately touches — project, state, priority,
        assignee — so a service that logs a transition does not issue four more queries.
        """
        return (
            Task.objects.select_related("project", "workflow_state", "priority", "assignee")
            .filter(code=task_code)
            .first()
        )

    def get_for_update(self, task_code: str) -> Task | None:
        """Return the task row-locked for the rest of the transaction, or ``None``.

        ``of=("self",)`` locks the task alone: locking the joined project and catalog rows
        as well would serialize unrelated tasks of the same project behind each other.
        """
        return (
            Task.objects.select_for_update(of=("self",))
            .select_related("project", "workflow_state", "priority", "assignee")
            .filter(code=task_code)
            .first()
        )

    def list_for_project(self, project_id: int) -> list[Task]:
        """Return every task of a project ordered by state then code, for the detail view."""
        return list(
            Task.objects.filter(project_id=project_id)
            .select_related("workflow_state", "priority", "assignee")
            .order_by("workflow_state__order", "code")
        )

    def counts_for_project(self, project_id: int, *, today: date) -> ProjectTaskCounts:
        """Return the four task counts the priority engine and the snapshot both read.

        One grouped query rather than four round trips, and — more importantly — one place
        where "open" is defined. ``overdue`` and ``urgent`` are counted *within* the open
        set, so a task that is past its due date but cancelled does not keep inflating the
        ``overdue_work`` signal forever. ``today`` is a parameter because the engine's
        notion of now must be the same one that produced the rest of its ``SignalInput``.

        Args:
            project_id: Primary key of the project, not its business code.
            today: The date "overdue" is measured against, in the caller's timezone.

        Returns:
            Counts that are all zero for a project with no tasks, never ``None``.
        """
        closed_categories = (StateCategory.DONE, StateCategory.CANCELLED)
        is_open = ~Q(workflow_state__category__in=closed_categories)
        aggregates = Task.objects.filter(project_id=project_id).aggregate(
            open_task_count=Count("pk", filter=is_open),
            overdue_task_count=Count("pk", filter=is_open & Q(due_date__lt=today)),
            urgent_open_task_count=Count("pk", filter=is_open & Q(priority__is_urgent=True)),
            blocked_task_count=Count(
                "pk", filter=Q(workflow_state__category=StateCategory.BLOCKED)
            ),
        )
        return ProjectTaskCounts(**aggregates)

    def dependency_adjacency_for_project(self, project_id: int) -> dict[str, tuple[str, ...]]:
        """Return the resolved dependency edges of one project, keyed by dependent code.

        The shape the pure cycle check consumes: unresolved rows contribute nothing because
        a ``raw_label`` cannot participate in a loop, and the scope is one project because
        a dependency may not cross projects (``DATA_MODEL`` §9.3). Tasks with no
        prerequisites are simply absent from the mapping.
        """
        edges = TaskDependency.objects.filter(
            task__project_id=project_id, depends_on__isnull=False
        ).values_list("task__code", "depends_on__code")

        adjacency: defaultdict[str, list[str]] = defaultdict(list)
        for task_code, depends_on_code in edges:
            adjacency[task_code].append(depends_on_code)
        return {task_code: tuple(targets) for task_code, targets in adjacency.items()}


class BlockerRepository:
    """Reads over ``work_blocker``. Openness is always ``resolved_at IS NULL``."""

    def get_for_update(self, blocker_id: int) -> Blocker | None:
        """Return the blocker row-locked, or ``None``.

        The lock is what makes "already resolved" a real check: without it two concurrent
        resolutions both read an open row and the second overwrites the first's reason.
        """
        return (
            Blocker.objects.select_for_update(of=("self",))
            .select_related("project", "task", "owner")
            .filter(pk=blocker_id)
            .first()
        )

    def open_for_project(self, project_id: int) -> list[Blocker]:
        """Return the project's open blockers, oldest first.

        The open-blockers panel, the ``blockage`` signal and the ``IsBlocked``
        specification all read this one query — hence oldest first, which is the order the
        panel needs and the order that makes ``[0]`` the blocker driving the score.
        """
        return list(
            Blocker.objects.filter(project_id=project_id, resolved_at__isnull=True)
            .select_related("owner", "task")
            .order_by("raised_at")
        )

    def open_summary_for_project(self, project_id: int) -> OpenBlockerSummary:
        """Return how many blockers are open and when the oldest was raised.

        The counting form of :meth:`open_for_project`, for the snapshot rebuild and the
        priority engine, which need the number and the age but never the prose.
        ``oldest_raised_at`` is ``None`` exactly when the count is zero.
        """
        open_blockers = Blocker.objects.filter(project_id=project_id, resolved_at__isnull=True)
        aggregates = open_blockers.aggregate(
            open_blocker_count=Count("pk"),
            oldest_raised_at=Min("raised_at"),
        )
        return OpenBlockerSummary(**aggregates)


class NoteRepository:
    """Reads over ``work_note``."""

    def recent_for_project(self, project_id: int, *, limit: int) -> list[Note]:
        """Return the newest notes of a project, task-scoped ones included.

        Backed by the ``(project, -created_at)`` index, so the ``limit`` stops the scan
        after that many index entries however large the table grows.
        """
        return list(
            Note.objects.filter(project_id=project_id)
            .select_related("task")
            .order_by("-created_at")[:limit]
        )


def project_by_code(project_code: str) -> Project | None:
    """Return the project with this business code, or ``None``.

    One of the three reads ``work`` makes into another context. It goes through
    ``Project``, which ``DATA_MODEL`` §8 names as the published aggregate root, and never
    into ``portfolio``'s internals.
    """
    return Project.objects.select_related("engagement_type").filter(code=project_code).first()


def priority_by_code(priority_code: str) -> Priority | None:
    """Return the ``catalog.Priority`` with this code, or ``None``.

    Inactive rows still resolve: a priority is soft-retired, and an existing task must keep
    rendering the priority it was created with.
    """
    return Priority.objects.filter(code=priority_code).first()


task_repository = TaskRepository()
blocker_repository = BlockerRepository()
note_repository = NoteRepository()
