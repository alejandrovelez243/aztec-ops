"""Persistence for the ``work`` context: tasks, dependencies, blockers and notes.

Fields, constraints, indexes, ``__str__`` — and the named queries, which live on each
model's ``QuerySet`` and reach callers through its manager (CLAUDE.md rule 6). A named
query here is a *business definition*, not a filter: "open" is ``WorkflowState.category NOT
IN (DONE, CANCELLED)`` and "open blocker" is ``resolved_at IS NULL``, and those definitions
drift the moment they are written twice. On the queryset they compose —
``Task.objects.for_project(pk).open().overdue(today)`` is one lazy query — where a
module-level ``open_tasks_for(project)`` would materialise and force a new function for
every new combination.

Every method returns its own queryset type so the chain stays typed, except the few that
must materialise (``counts``, ``summary``, ``adjacency``); each of those says so in its
docstring, because it ends the chain.

Business *rules* are not here. Every rule that spans two rows lives in ``services/`` — the
cross-row invariants this module cannot express (a blocker's project matching its task's,
the acyclicity of the dependency graph) are listed in ``DATA_MODEL`` §9.3 with the reason
each one is enforced in Python.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import timedelta
from typing import TYPE_CHECKING, Any, Final

from django.conf import settings
from django.db import connection, models
from django.db.models import Count, F, Min, Q

from apps.shared.refs import TaxonomyRef
from apps.work.domain.value_objects import BlockerKind, OpenBlockerSummary, ProjectTaskCounts
from apps.work.domain.views import (
    BlockerView,
    DependencyRef,
    NoteView,
    TaskDetailView,
    TaskProjectRef,
    TaskView,
)
from apps.workflow.models import StateCategory

if TYPE_CHECKING:
    from datetime import date, datetime

    from apps.accounts.models import User
    from apps.portfolio.models import Project
    from apps.workflow.domain.views import TransitionOption

#: ``choices`` rendered from the domain enum, so the closed set is declared once. It is a
#: structural vocabulary, not a taxonomy: adding a member is a migration, by design.
BLOCKER_KIND_CHOICES = [(kind.value, kind.label) for kind in BlockerKind]

#: PostgreSQL sequences that mint the business codes, created by raw SQL in migration
#: ``work.0001_initial`` with a matching ``reverse_sql``.
BLOCKER_CODE_SEQUENCE: Final = "work_blocker_code_seq"
NOTE_CODE_SEQUENCE: Final = "work_note_code_seq"

BLOCKER_CODE_PREFIX: Final = "BLK"
NOTE_CODE_PREFIX: Final = "NOTE"

#: Minimum width of the numeric part (``BLK-0142``). It is a floor, not a limit: the 10000th
#: blocker is ``BLK-10000`` and stays sortable as a number, which is why nothing truncates here.
CODE_DIGITS: Final = 4

#: ``WorkflowState.category`` values that mean the work has stopped for good. "Open" is the
#: complement of this set, read from the workflow context's own vocabulary rather than re-listed
#: as state codes — a state invented from the admin is open by default instead of being invisible
#: to every count (DATA_MODEL §12).
CLOSED_CATEGORIES: Final = (StateCategory.DONE, StateCategory.CANCELLED)

#: Separator between a project's code and its task number (``PRJ-01-T03``). A task code is scoped
#: to its project on purpose — see :meth:`TaskQuerySet.next_code_for`.
TASK_CODE_INFIX: Final = "-T"

#: Minimum width of a task's number inside its project. A floor, not a limit: a project's 100th
#: task is ``-T100``.
TASK_CODE_DIGITS: Final = 2

#: The joins every projection of a task reads. Kept in one bundle so no caller half-populates a
#: task and then pays for the rest one lazy query at a time.
TASK_RELATIONS: Final = ("project", "workflow_state", "priority", "assignee")


def _next_business_code(sequence: str, prefix: str) -> str:
    """Draw the next value of a PostgreSQL sequence and render it as a business code.

    A sequence rather than ``max(pk) + 1`` or a Python counter, and the reason is concurrency:
    both alternatives read a value, decide, then write, so two transactions inserting at the same
    instant compute the same number and the second one either dies on the unique constraint or —
    worse, if the constraint were ever dropped — reuses a code that already names another row.
    ``nextval`` is atomic and non-transactional: it never hands the same number to two callers,
    even inside concurrent transactions. Its counterpart is that a rolled-back insert burns its
    number, which is correct here — codes are never reused and never renumbered, so a gap in the
    series is a fact about the operation, not a defect to be compacted away.

    Args:
        sequence: Name of the sequence created by the migration.
        prefix: Code prefix (``BLK``, ``NOTE``).

    Returns:
        The rendered code, zero-padded to :data:`CODE_DIGITS` and growing past it naturally.

    Raises:
        django.db.utils.ProgrammingError: The sequence does not exist, which means the migration
            that creates it has not been applied. Failing here is deliberate: the alternative is
            a row inserted with no code.
    """
    with connection.cursor() as cursor:
        cursor.execute("SELECT nextval(%s)", [sequence])
        row = cursor.fetchone()
    return f"{prefix}-{int(row[0]):0{CODE_DIGITS}d}"


class TaskQuerySet(models.QuerySet["Task"]):
    """The vocabulary of ``work_task``: what "open", "overdue" and "urgent" mean, once.

    Every method is chainable, so the three counts the prioritization engine needs are the
    same chain counted three ways rather than three bespoke queries. :meth:`counts` is the
    exception and says so: it aggregates and ends the chain.
    """

    def with_relations(self) -> TaskQuerySet:
        """Join the rows every caller of a task immediately touches.

        Project, state, priority and assignee are rendered or logged wherever a task is, so
        leaving them lazy turns one read into four.
        """
        return self.select_related(*TASK_RELATIONS)

    def locked(self) -> TaskQuerySet:
        """Row-lock the selected tasks for the rest of the transaction.

        ``of=("self",)`` locks the task alone: locking the joined project and catalog rows as
        well would serialize unrelated tasks of the same project behind each other. Requires an
        open transaction — outside one, evaluating this raises ``TransactionManagementError``.
        """
        return self.select_for_update(of=("self",))

    def for_code(self, task_code: str) -> TaskQuerySet:
        """Narrow to the task carrying this business code (``TSK-0007``), or to nothing."""
        return self.filter(code=task_code)

    def for_project(self, project: Project | int | str) -> TaskQuerySet:
        """Narrow to the tasks of one project, named by row, primary key or business code.

        Args:
            project: The project instance, its primary key, or its ``code``. All three occur —
                a service holds the row, an aggregation holds the id, a caller from another
                context holds only the code — and resolving them here is what keeps the join
                spelling out of six call sites.
        """
        if isinstance(project, str):
            return self.filter(project__code=project)
        if isinstance(project, int):
            return self.filter(project_id=project)
        return self.filter(project=project)

    def assigned_to(self, user: User | str) -> TaskQuerySet:
        """Narrow to the tasks carried by one person, named by row or by ``User.code``.

        Unassigned tasks are excluded by construction: nobody carries them, which is a
        different fact from carrying nothing.
        """
        if isinstance(user, str):
            return self.filter(assignee__code=user)
        return self.filter(assignee=user)

    def in_state_category(self, category: str) -> TaskQuerySet:
        """Narrow to the tasks whose state belongs to one ``StateCategory``.

        The general form of :meth:`open`, :meth:`closed` and :meth:`blocked`, for the callers
        that ask about a category those three do not name — ``IN_PROGRESS``, say.
        """
        return self.filter(workflow_state__category=category)

    def open(self) -> TaskQuerySet:
        """Tasks still live: their state's category is neither ``DONE`` nor ``CANCELLED``."""
        return self.exclude(workflow_state__category__in=CLOSED_CATEGORIES)

    def closed(self) -> TaskQuerySet:
        """Tasks that have stopped for good, whether they were finished or abandoned."""
        return self.filter(workflow_state__category__in=CLOSED_CATEGORIES)

    def blocked(self) -> TaskQuerySet:
        """Tasks sitting in a ``BLOCKED`` state.

        This is the workflow's answer, not the blocker table's: a task can be impeded without
        anyone having raised a ``Blocker`` row, and the state is what the board shows.
        """
        return self.in_state_category(StateCategory.BLOCKED)

    def overdue(self, as_of: date) -> TaskQuerySet:
        """Tasks whose due date has already passed at ``as_of``.

        Undated tasks are never overdue — no date is not a missed one. ``as_of`` is required
        rather than defaulted to today because the engine's notion of now must be the one that
        produced the rest of its ``SignalInput``; a replay must reproduce the old answer.

        Chain onto :meth:`open` to exclude work that is past its date but already cancelled,
        which would otherwise inflate the overdue signal forever.
        """
        return self.filter(due_date__lt=as_of)

    def urgent(self) -> TaskQuerySet:
        """Tasks whose priority is flagged urgent in the taxonomy.

        Read from ``Priority.is_urgent`` and never from a list of codes, so the operation can
        add a priority above ``critica`` from the admin without a migration and without a grep
        for hardcoded codes (CLAUDE.md rule 1).
        """
        return self.filter(priority__is_urgent=True)

    def in_board_order(self) -> TaskQuerySet:
        """Order by workflow position then code, the order the project detail view renders."""
        return self.order_by("workflow_state__order", "code")

    def in_attention_order(self) -> TaskQuerySet:
        """Order the way an operator triages: most severe first, then soonest due, then code.

        Severity is ``Priority.weight`` descending, read from the taxonomy rather than from a
        list of codes, so a priority inserted above ``critica`` sorts correctly with no code
        change (CLAUDE.md rule 1). Undated tasks sort after dated ones — an unset due date is not
        an infinitely urgent one. ``code`` is appended as the final key so a page boundary can
        never repeat or drop a row when two tasks tie on both.
        """
        return self.order_by("-priority__weight", F("due_date").asc(nulls_last=True), "code")

    def with_dependencies(self) -> TaskQuerySet:
        """Prefetch each task's prerequisites and the task each one points at.

        Without it, rendering a page of tasks costs two queries per row. Opt-in rather than
        automatic: the counts and the overdue aggregates read no dependency at all.
        """
        return self.prefetch_related("dependencies__depends_on")

    def search(self, term: str) -> TaskQuerySet:
        """Narrow to tasks whose code or title contains ``term``, case-insensitively.

        Deliberately not a search over ``detail`` or ``last_progress``: those are long prose
        fields, and a substring scan over them turns the task list into a sequential read while
        matching text no operator was looking for.

        Args:
            term: Free text. An empty or whitespace-only term does not filter, so a cleared
                search box shows everything rather than nothing.
        """
        if not term.strip():
            return self
        return self.filter(Q(code__icontains=term) | Q(title__icontains=term))

    def next_code_for(self, project_code: str) -> str:
        """Mint the next task code inside one project — ``PRJ-01-T03``.

        **Ends the chain**: it materialises the project's existing codes. Task codes are scoped to
        their project rather than drawn from a global sequence like ``Blocker`` and ``Note``,
        because the source data numbers them that way and an operator reads ``PRJ-01-T03`` as "the
        third task of PRJ-01" — a global ``TSK-0417`` would lose that.

        The numbering is derived from the highest suffix already present rather than from a count,
        so deleting a task never causes the next one to reuse a retired code.

        This is *not* collision-proof under concurrency: two simultaneous creations can read the
        same maximum. The unique constraint on ``code`` catches that, and the losing transaction
        rolls back — acceptable while task creation is a single operator clicking a button, and the
        reason a real sequence would be the fix if it ever stops being one.

        Args:
            project_code: ``portfolio.Project.code`` the task belongs to.

        Returns:
            The next free code for that project.
        """
        prefix = f"{project_code}{TASK_CODE_INFIX}"
        used = self.for_project(project_code).values_list("code", flat=True)
        highest = 0
        for code in used:
            suffix = str(code).removeprefix(prefix)
            if suffix.isdigit():
                highest = max(highest, int(suffix))
        return f"{prefix}{highest + 1:0{TASK_CODE_DIGITS}d}"

    def as_views(self, *, today: date) -> tuple[TaskView, ...]:
        """Materialise the selection as the projections the API renders.

        **Ends the chain**: the query executes here, so nothing downstream can narrow the set
        after the response shape was decided, and no field is resolved lazily after the
        transaction that read the rows has closed.

        Chain :meth:`with_relations` and :meth:`with_dependencies` first; on rows fetched without
        them this is still correct but costs six queries per task.

        Args:
            today: The date every ``is_overdue`` in this response is measured against. One
                instant for the whole page, so two rows cannot be aged against two clocks.
        """
        return tuple(task.to_view(today=today) for task in self)

    def counts(self, *, today: date) -> ProjectTaskCounts:
        """Aggregate the five task counts the priority engine and the snapshot both read.

        **Ends the chain**: this materialises one grouped query and returns a frozen value
        object, not a queryset. Chain the scope first — ``Task.objects.for_project(pk)``.

        One query rather than five round trips, and the definitions are the same ones
        :meth:`open`, :meth:`overdue`, :meth:`urgent`, :meth:`blocked` and
        :meth:`in_state_category` express: overdue and urgent are counted *within* the open set,
        so a task past its due date but cancelled stops inflating ``overdue_work``.

        Args:
            today: The date "overdue" is measured against, in the caller's timezone.

        Returns:
            Counts that are all zero over an empty selection, never ``None``.
        """
        is_open = ~Q(workflow_state__category__in=CLOSED_CATEGORIES)
        aggregates = self.aggregate(
            open_task_count=Count("pk", filter=is_open),
            overdue_task_count=Count("pk", filter=is_open & Q(due_date__lt=today)),
            urgent_open_task_count=Count("pk", filter=is_open & Q(priority__is_urgent=True)),
            blocked_task_count=Count(
                "pk", filter=Q(workflow_state__category=StateCategory.BLOCKED)
            ),
            in_progress_task_count=Count(
                "pk", filter=Q(workflow_state__category=StateCategory.IN_PROGRESS)
            ),
        )
        return ProjectTaskCounts(**aggregates)


class Task(models.Model):
    """A unit of work inside a project, with its own workflow state.

    ``workflow_state`` is written only by the transition service; nothing else assigns it,
    which is what keeps the legal moves a table rather than a set of ``if`` statements. A
    task has no life outside its project, hence the cascade, while the assignee is nulled
    on delete because the historical fact that the work existed must outlive the roster.

    ``assignee`` points at ``AUTH_USER_MODEL``: the person assigned the work is the same
    person who signs in to move it, so there is one row per person and no nullable hop to
    traverse before a permission check can name the requester.
    """

    code = models.CharField(max_length=16)
    project = models.ForeignKey("portfolio.Project", on_delete=models.CASCADE, related_name="tasks")
    assignee = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="tasks",
    )
    priority = models.ForeignKey("catalog.Priority", on_delete=models.PROTECT, related_name="tasks")
    workflow_state = models.ForeignKey(
        "workflow.WorkflowState", on_delete=models.PROTECT, related_name="tasks"
    )
    due_date = models.DateField(null=True, blank=True)
    title = models.CharField(max_length=200)
    detail = models.TextField(default="", blank=True)
    last_progress = models.CharField(max_length=255, default="", blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = TaskQuerySet.as_manager()

    class Meta:
        verbose_name = "task"
        verbose_name_plural = "tasks"
        ordering = ["code"]
        constraints = [
            models.UniqueConstraint(fields=["code"], name="work_task_code_unique"),
        ]
        indexes = [
            models.Index(fields=["project", "workflow_state"], name="work_task_project_state"),
            models.Index(fields=["assignee", "workflow_state"], name="work_task_assignee_state"),
            models.Index(fields=["due_date"], name="work_task_due_date"),
        ]

    def __str__(self) -> str:
        return f"{self.code} {self.title}"

    def to_view(self, *, today: date) -> TaskView:
        """Describe this task as the projection the API returns.

        ``is_overdue`` is derived here and never read from a column. The source spreadsheet's
        ``Si``/``No`` overdue flag is deliberately not imported (ARCHITECTURE §10.1): a stored
        flag is wrong the morning after it was written, and two readers of the same row would
        then disagree about whether the task is late.

        Reads ``priority``, ``workflow_state``, ``assignee`` and the prefetched ``dependencies``;
        chain :meth:`TaskQuerySet.with_relations` and :meth:`TaskQuerySet.with_dependencies` on
        the query that produced this instance.

        Args:
            today: The date lateness is measured against, supplied by the caller so a whole page
                — and a replay — uses one instant.

        Returns:
            The task as an immutable :class:`~apps.work.domain.views.TaskView`.
        """
        return TaskView(
            code=self.code,
            title=self.title,
            assignee=self.assignee.to_ref() if self.assignee else None,
            priority=TaxonomyRef.of(
                code=self.priority.code,
                label=self.priority.label,
                color=self.priority.color,
            ),
            state=self.workflow_state.to_ref(),
            due_date=self.due_date,
            is_overdue=self.due_date is not None and self.due_date < today,
            last_progress=self.last_progress,
            dependencies=tuple(edge.to_ref() for edge in self.dependencies.all()),
        )

    def to_detail_view(
        self,
        *,
        today: date,
        transitions: tuple[TransitionOption, ...],
        notes: tuple[NoteView, ...],
    ) -> TaskDetailView:
        """Describe this task as the single-screen projection the task detail renders.

        The task maps its own columns — including the reference to the project it hangs off, which
        is a projection of a field it already holds. The two collections it cannot derive are
        passed in: ``transitions`` belongs to the workflow context and ``notes`` to a sibling table,
        and a model that queried either would put an N+1 inside a projection and make the legal-move
        set a property of the row rather than of the graph.

        Reads ``project``, ``priority``, ``workflow_state``, ``assignee`` and the prefetched
        ``dependencies``; chain :meth:`TaskQuerySet.with_relations` and
        :meth:`TaskQuerySet.with_dependencies` on the query that produced this instance, or the
        response costs six queries.

        Args:
            today: The date ``is_overdue`` is measured against, supplied by the caller so the whole
                response — and a replay of it — uses one instant.
            transitions: The active edges leaving this task's current state, already projected.
                Empty means the task is terminal, which is a legitimate answer and not a defect.
            notes: The task's comments, newest first, already projected.

        Returns:
            The task as an immutable :class:`~apps.work.domain.views.TaskDetailView`.
        """
        return TaskDetailView(
            code=self.code,
            title=self.title,
            detail=self.detail,
            project=TaskProjectRef(code=self.project.code, name=self.project.name),
            assignee=self.assignee.to_ref() if self.assignee else None,
            priority=TaxonomyRef.of(
                code=self.priority.code,
                label=self.priority.label,
                color=self.priority.color,
            ),
            state=self.workflow_state.to_ref(),
            due_date=self.due_date,
            is_overdue=self.due_date is not None and self.due_date < today,
            last_progress=self.last_progress,
            dependencies=tuple(edge.to_ref() for edge in self.dependencies.all()),
            transitions=transitions,
            notes=notes,
            created_at=self.created_at,
            updated_at=self.updated_at,
        )


class TaskDependencyQuerySet(models.QuerySet["TaskDependency"]):
    """Reads over the dependency edges, whose only consumer is the pure cycle check."""

    def for_project(self, project: Project | int | str) -> TaskDependencyQuerySet:
        """Narrow to the edges of one project, named by row, primary key or business code.

        A dependency may not cross projects (``DATA_MODEL`` §9.3), so the project *is* the
        natural scope of the graph: no correct caller ever wants the edges of two at once.
        """
        if isinstance(project, str):
            return self.filter(task__project__code=project)
        if isinstance(project, int):
            return self.filter(task__project_id=project)
        return self.filter(task__project=project)

    def resolved(self) -> TaskDependencyQuerySet:
        """Edges that point at a real task, excluding the ones still carrying only prose.

        ``depends_on IS NOT NULL`` rather than the stored ``is_resolved`` mirror: the column
        exists so the admin can filter without a null scan, but the pointer is the truth.
        """
        return self.filter(depends_on__isnull=False)

    def adjacency(self) -> dict[str, tuple[str, ...]]:
        """Aggregate the selected edges into the mapping the cycle check consumes.

        **Ends the chain**: this materialises. Chain the scope and the resolution first —
        ``TaskDependency.objects.for_project(pk).resolved().adjacency()``.

        Keyed by dependent code, valued by the codes it waits on. Tasks with no prerequisites
        are simply absent rather than mapped to an empty tuple, which is the shape
        ``domain.dependencies`` walks.
        """
        edges = self.values_list("task__code", "depends_on__code")

        adjacency: defaultdict[str, list[str]] = defaultdict(list)
        for task_code, depends_on_code in edges:
            adjacency[task_code].append(depends_on_code)
        return {task_code: tuple(targets) for task_code, targets in adjacency.items()}


class TaskDependency(models.Model):
    """A "this cannot start until that is done" edge, resolved to a task or still free text.

    ``depends_on`` and ``raw_label`` both exist because 61 of the 82 source tasks name
    their prerequisite by title rather than by code, and many of those titles match
    nothing. An unresolvable dependency is preserved as prose instead of being dropped, and
    ``is_resolved`` is the stored form of ``depends_on IS NOT NULL`` so the admin's
    "unresolved dependencies" view is one filter rather than a null scan.
    """

    task = models.ForeignKey(Task, on_delete=models.CASCADE, related_name="dependencies")
    depends_on = models.ForeignKey(
        Task, on_delete=models.SET_NULL, null=True, blank=True, related_name="dependents"
    )
    raw_label = models.CharField(max_length=255, default="", blank=True)
    is_resolved = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    objects = TaskDependencyQuerySet.as_manager()

    class Meta:
        verbose_name = "task dependency"
        verbose_name_plural = "task dependencies"
        ordering = ["task", "pk"]
        constraints = [
            # Unresolved rows repeat freely: the same prose legitimately appears twice.
            models.UniqueConstraint(
                fields=["task", "depends_on"],
                condition=Q(depends_on__isnull=False),
                name="work_taskdep_edge_unique",
            ),
            models.CheckConstraint(
                condition=Q(depends_on__isnull=False) | ~Q(raw_label=""),
                name="work_taskdep_has_target_or_label",
            ),
            # The only cycle a CHECK can catch; the general case is domain/dependencies.py.
            models.CheckConstraint(
                condition=Q(depends_on__isnull=True) | ~Q(depends_on=F("task")),
                name="work_taskdep_not_self",
            ),
        ]

    def __str__(self) -> str:
        target = self.depends_on.code if self.depends_on is not None else self.raw_label
        return f"{self.task.code} depends on {target}"

    def to_ref(self) -> DependencyRef:
        """Describe this edge as the projection a task carries.

        Both halves travel: an edge that resolved still ships its ``raw_label``, because that is
        the sentence the operation wrote and the resolution is an inference on top of it. Reads
        ``depends_on``, so callers chain :meth:`TaskQuerySet.with_dependencies`.

        Returns:
            The prerequisite as an immutable :class:`~apps.work.domain.views.DependencyRef`.
        """
        return DependencyRef(
            task_code=self.depends_on.code if self.depends_on is not None else None,
            raw_label=self.raw_label,
        )


class BlockerQuerySet(models.QuerySet["Blocker"]):
    """Reads over ``work_blocker``. Openness is ``resolved_at IS NULL`` and nothing else."""

    def with_relations(self) -> BlockerQuerySet:
        """Join owner, task and project — the three rows the panel and the audit both read."""
        return self.select_related("project", "task", "owner")

    def locked(self) -> BlockerQuerySet:
        """Row-lock the selected blockers for the rest of the transaction.

        The lock is what makes "already resolved" a real check: without it two concurrent
        resolutions both read an open row and the second overwrites the first's reason.
        """
        return self.select_for_update(of=("self",))

    def for_project(self, project: Project | int | str) -> BlockerQuerySet:
        """Narrow to the blockers of one project, named by row, primary key or business code.

        ``project`` is populated even on a task-level blocker, so this never joins ``Task``.
        """
        if isinstance(project, str):
            return self.filter(project__code=project)
        if isinstance(project, int):
            return self.filter(project_id=project)
        return self.filter(project=project)

    def open(self) -> BlockerQuerySet:
        """Blockers nobody has resolved yet — the one definition of an impediment that counts.

        Backed by the partial ``work_blocker_open`` index, so the panel, the ``blockage``
        signal and the ``IsBlocked`` specification all read the same rows the same way.
        """
        return self.filter(resolved_at__isnull=True)

    def resolved(self) -> BlockerQuerySet:
        """Blockers that were closed, each one carrying the reason it was closed with."""
        return self.filter(resolved_at__isnull=False)

    def older_than(self, days: int, *, as_of: datetime) -> BlockerQuerySet:
        """Blockers raised more than ``days`` before ``as_of``.

        Age is measured from ``raised_at`` and never from the resolution, so this composes with
        both :meth:`open` and :meth:`resolved`. ``as_of`` is a parameter, not ``now()``, because
        a query whose answer depends on when it runs cannot be replayed or tested.
        """
        return self.filter(raised_at__lt=as_of - timedelta(days=days))

    def oldest_first(self) -> BlockerQuerySet:
        """Order by when the blocker was raised, oldest first.

        The order the panel renders and the order that makes ``[0]`` the blocker driving the
        score — which is why it is not left to the model's newest-first default.
        """
        return self.order_by("raised_at")

    def in_panel_order(self) -> BlockerQuerySet:
        """Order as the detail view renders: open first, then the newest within each group.

        Open before resolved because an impediment nobody has cleared is the only one anybody can
        act on; ``-raised_at`` inside each group because the newest is the one the reader has not
        seen yet. ``-pk`` breaks a tie so a page boundary is stable.
        """
        return self.order_by(F("resolved_at").asc(nulls_first=True), "-raised_at", "-pk")

    def as_views(self, *, now: datetime) -> tuple[BlockerView, ...]:
        """Materialise the selection as the projections the blocker panel renders.

        **Ends the chain**: the query executes here. Chain :meth:`with_relations` first, or each
        row costs three lazy queries.

        Args:
            now: The instant every ``age_days`` in this response is measured against, so two
                blockers rendered together cannot be aged against two different clocks.
        """
        return tuple(blocker.to_view(now=now) for blocker in self)

    def summary(self) -> OpenBlockerSummary:
        """Aggregate how many blockers are selected and when the oldest was raised.

        **Ends the chain**: this materialises. Chain the scope first —
        ``Blocker.objects.for_project(pk).open().summary()``.

        The counting form of the panel's read, for the snapshot rebuild and the priority
        engine, which need the number and the age but never the prose. ``oldest_raised_at`` is
        ``None`` exactly when the count is zero. The value object names its count ``open`` after
        the only selection worth summarising, so chain :meth:`open` before calling it.
        """
        aggregates = self.aggregate(
            open_blocker_count=Count("pk"),
            oldest_raised_at=Min("raised_at"),
        )
        return OpenBlockerSummary(**aggregates)


class Blocker(models.Model):
    """An impediment as a row, not as a substring of a notes field.

    Openness is ``resolved_at IS NULL`` and nothing else, which is what lets the blockage
    signal, the risk evaluator and the open-blockers panel agree on one definition.
    ``project`` is always populated — even for a task-level blocker, where the service
    copies it from the task — so any consumer can resolve a blocker to a portfolio row
    without joining through ``Task``.

    ``code`` is the business identifier the event envelope carries as ``entity.id``
    (EVENTS.md §1): a consumer in another context addresses a blocker by ``BLK-0142`` and
    never needs a foreign key into ``work``. It is assigned once, on insert, from
    ``work_blocker_code_seq``.

    This row is **not** append-only, unlike ``ActivityRecord``: resolving a blocker is an
    update of ``resolved_at`` and ``resolution_reason``, so ``save()`` must keep accepting
    updates. Only ``code`` is immutable — it is minted on the insert and never recomputed,
    because a code that can be renumbered is a code no audit trail can rely on.
    """

    code = models.CharField(max_length=16, blank=True)
    project = models.ForeignKey(
        "portfolio.Project", on_delete=models.CASCADE, related_name="blockers"
    )
    task = models.ForeignKey(
        Task, on_delete=models.CASCADE, null=True, blank=True, related_name="blockers"
    )
    description = models.TextField()
    kind = models.CharField(max_length=24, choices=BLOCKER_KIND_CHOICES)
    raised_at = models.DateTimeField(auto_now_add=True)
    resolved_at = models.DateTimeField(null=True, blank=True)
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="blockers",
    )
    resolution_reason = models.CharField(max_length=255, default="", blank=True)

    objects = BlockerQuerySet.as_manager()

    class Meta:
        verbose_name = "blocker"
        verbose_name_plural = "blockers"
        ordering = ["-raised_at"]
        constraints = [
            models.UniqueConstraint(fields=["code"], name="work_blocker_code_unique"),
            models.CheckConstraint(
                condition=Q(resolved_at__isnull=True) | Q(resolved_at__gte=F("raised_at")),
                name="work_blocker_resolved_after_raised",
            ),
            # Closing a blocker without saying how is how blockers come back.
            models.CheckConstraint(
                condition=Q(resolved_at__isnull=True) | ~Q(resolution_reason=""),
                name="work_blocker_resolution_reason",
            ),
        ]
        indexes = [
            # Partial: a resolved blocker is read by neither the panel nor the signal.
            models.Index(
                fields=["project"],
                condition=Q(resolved_at__isnull=True),
                name="work_blocker_open",
            ),
            models.Index(fields=["kind"], name="work_blocker_kind"),
            models.Index(fields=["raised_at"], name="work_blocker_raised_at"),
        ]

    def __str__(self) -> str:
        state = "resolved" if self.resolved_at else "open"
        return f"{self.code} {self.kind} blocker ({state})"

    def save(self, *args: Any, **kwargs: Any) -> None:
        """Mint ``code`` on the insert, then save normally.

        The code is drawn from ``work_blocker_code_seq`` and only when the row is being
        inserted, which is what makes it immutable: an update — resolving the blocker — never
        reaches the generator, so the identifier a consumer already saw on ``blocker.raised``
        still names the same row on ``blocker.resolved``. This is *not* the append-only
        ``ActivityRecord`` guard: updates are legitimate here and are never refused.

        An explicit ``code`` is honoured so a fixture can pin one; ``loaddata`` bypasses this
        method entirely (``save_base(raw=True)``), so seeds must carry their own codes.

        Raises:
            django.db.utils.IntegrityError: The explicit code collides with an existing row.
                Generated codes cannot collide — ``nextval`` never repeats.
        """
        if self._state.adding and not self.code:
            self.code = _next_business_code(BLOCKER_CODE_SEQUENCE, BLOCKER_CODE_PREFIX)
        super().save(*args, **kwargs)

    def to_view(self, *, now: datetime) -> BlockerView:
        """Describe this blocker as the projection the API returns.

        ``age_days`` is derived, never stored: it changes every midnight, and a stored copy would
        be the number the panel shows while the ``blockage`` signal used a fresh one. It is
        measured from ``raised_at`` to ``now`` for an open blocker and to ``resolved_at`` for a
        closed one, so a resolved row keeps saying how long it actually blocked the work rather
        than growing forever.

        Reads ``task`` and ``owner``; chain :meth:`BlockerQuerySet.with_relations`.

        Args:
            now: The instant an open blocker's age is measured against.

        Returns:
            The blocker as an immutable :class:`~apps.work.domain.views.BlockerView`.
        """
        until = self.resolved_at if self.resolved_at is not None else now
        return BlockerView(
            id=self.pk,
            code=self.code,
            kind=self.kind,
            description=self.description,
            owner=self.owner.to_ref() if self.owner else None,
            raised_at=self.raised_at,
            resolved_at=self.resolved_at,
            resolution_reason=self.resolution_reason,
            age_days=max(0, (until - self.raised_at).days),
            task_code=self.task.code if self.task else None,
        )


class NoteQuerySet(models.QuerySet["Note"]):
    """Reads over ``work_note``, all of them chronological."""

    def for_project(self, project: Project | int | str) -> NoteQuerySet:
        """Narrow to a project's notes, task-scoped ones included.

        ``project`` is copied onto task-level notes when they are written, so the project
        timeline never joins ``Task`` to find them.
        """
        if isinstance(project, str):
            return self.filter(project__code=project)
        if isinstance(project, int):
            return self.filter(project_id=project)
        return self.filter(project=project)

    def for_task(self, task: Task | int | str) -> NoteQuerySet:
        """Narrow to the comments written against one task, named by row, primary key or code.

        Strictly narrower than :meth:`for_project`: a project-level note carries no task and is
        excluded here, which is the point. The task detail shows the conversation about *this*
        piece of work, and folding the project's own timeline into it would make every task on the
        project look like it was being discussed.
        """
        if isinstance(task, str):
            return self.filter(task__code=task)
        if isinstance(task, int):
            return self.filter(task_id=task)
        return self.filter(task=task)

    def recent(self, limit: int) -> NoteQuerySet:
        """The newest ``limit`` notes of the current selection, newest first.

        Backed by the ``(project, -created_at)`` index when scoped by :meth:`for_project` and by
        the partial ``(task, -created_at)`` one when scoped by :meth:`for_task`, so ``limit`` stops
        the scan after that many index entries however large the table grows. Slicing is what makes
        this the last *filtering* step: the result is still lazy, but Django refuses further
        ``filter()`` calls on it, so chain the scope — ``Note.objects.for_project(pk).recent(20)``.
        """
        return self.select_related("task").order_by("-created_at")[:limit]

    def as_views(self) -> tuple[NoteView, ...]:
        """Materialise the selection as the projections the API renders.

        **Ends the chain**: the query executes here, so nothing downstream can narrow the set after
        the response shape was decided. Chain the scope and :meth:`recent` first —
        ``Note.objects.for_task(pk).recent(50).as_views()``.
        """
        return tuple(note.to_view() for note in self)


class Note(models.Model):
    """A chronological comment on a project, or on one task of it.

    ``author`` is a denormalized ``accounts.User.code`` rather than a foreign key, and it
    stays that way now that the assignee *is* a user: a note survives its author's row being
    deleted, and it also records authors who are not people at all — the engine and the
    consumers write as ``system``. ``project`` is always set, copied from the task for
    task-scoped notes, so the project timeline never needs a join to find them.

    ``code`` is the business identifier the ``note.added`` envelope carries as ``entity.id``
    (``NOTE-0391``), so a consumer addresses a note without a foreign key into ``work``. It is
    assigned once, on insert, from ``work_note_code_seq``.

    A note is not append-only at the database level — the body can be corrected — but its
    ``code`` is immutable: it is minted on the insert and never recomputed, because the code is
    what an already-published event points at.
    """

    code = models.CharField(max_length=16, blank=True)
    project = models.ForeignKey("portfolio.Project", on_delete=models.CASCADE, related_name="notes")
    task = models.ForeignKey(
        Task, on_delete=models.CASCADE, null=True, blank=True, related_name="notes"
    )
    body = models.TextField()
    author = models.CharField(max_length=32)
    created_at = models.DateTimeField(auto_now_add=True)

    objects = NoteQuerySet.as_manager()

    class Meta:
        verbose_name = "note"
        verbose_name_plural = "notes"
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(fields=["code"], name="work_note_code_unique"),
        ]
        indexes = [
            models.Index(fields=["project", "-created_at"], name="work_note_project_recent"),
            # Partial: most notes hang off the project alone, and the task detail's read never
            # looks at those. Indexing them would double the write cost of every project note to
            # serve a query that excludes it by definition.
            models.Index(
                fields=["task", "-created_at"],
                condition=Q(task__isnull=False),
                name="work_note_task_recent",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.code} by {self.author} at {self.created_at:%Y-%m-%d}"

    def save(self, *args: Any, **kwargs: Any) -> None:
        """Mint ``code`` on the insert, then save normally.

        Same contract as :meth:`Blocker.save`: the sequence is read only while the row is being
        inserted, so an edit to the body cannot renumber a note that a published ``note.added``
        event already names. Updates are allowed — this is not the append-only
        ``ActivityRecord`` guard.

        An explicit ``code`` is honoured so a fixture can pin one; ``loaddata`` bypasses this
        method entirely (``save_base(raw=True)``), so seeds must carry their own codes.

        Raises:
            django.db.utils.IntegrityError: The explicit code collides with an existing row.
                Generated codes cannot collide — ``nextval`` never repeats.
        """
        if self._state.adding and not self.code:
            self.code = _next_business_code(NOTE_CODE_SEQUENCE, NOTE_CODE_PREFIX)
        super().save(*args, **kwargs)

    def to_view(self) -> NoteView:
        """Describe this note as the projection the API returns.

        ``author`` stays the denormalized code rather than being resolved to a person: the column
        exists precisely so a note survives its author leaving the roster and so a consumer can
        write one as ``system``, and resolving it would fail for exactly those rows.

        Reads ``task``; chain ``select_related("task")`` when rendering many.

        Returns:
            The note as an immutable :class:`~apps.work.domain.views.NoteView`.
        """
        return NoteView(
            id=self.pk,
            code=self.code,
            body=self.body,
            author=self.author,
            created_at=self.created_at,
            task_code=self.task.code if self.task else None,
        )
