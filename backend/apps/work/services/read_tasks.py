"""Use case: read one page of a project's tasks.

Two queries whatever the filters: a ``COUNT(*)`` over the whole match so the client can page, and
one windowed scan. Everything the filters compare against is a ``code``, a ``category`` or a date —
never an operator-editable label (CLAUDE.md rule 1) — and ``is_overdue`` is derived from
``due_date`` against the caller's date rather than read from a column, because the spreadsheet's
own overdue flag was stale the day it was exported.
"""

from datetime import date

from pydantic import BaseModel, ConfigDict, Field

from apps.portfolio.models import Project
from apps.shared.ordering import resolve_ordering
from apps.shared.pagination import Page
from apps.work.domain.errors import ProjectNotFound
from apps.work.domain.views import TaskView
from apps.work.models import Task, TaskQuerySet

#: Signed field names ``GET /api/v1/projects/{code}/tasks`` accepts, mapped to what they sort.
#: ``priority`` sorts by ``Priority.weight`` and not by code, so a priority inserted above
#: ``critica`` from the admin sorts correctly with no code change.
TASK_ORDERING: dict[str, str] = {
    "due_date": "due_date",
    "priority": "priority__weight",
    "state": "workflow_state__order",
    "code": "code",
}

#: Applied when the caller names no ordering: severity first, then soonest due — the order an
#: operator triages in, which is why it is not simply ``code``.
DEFAULT_TASK_ORDERING = "attention"


class TaskFilters(BaseModel):
    """The facets and window of one task-list read.

    ``None`` means "do not filter". ``is_overdue`` is a *derived* facet: it compares ``due_date``
    against ``as_of`` inside the query, so it can never disagree with the ``is_overdue`` rendered
    on the rows it returns.

    ``is_archived`` is the one facet that is **not** optional and defaults to ``False``: removed
    tasks are out of the operation's attention by definition (ADR 0012), so "both at once" is not
    an answer this endpoint offers — a list mixing them would show an operator work that no longer
    counts toward anything the same size as work that does. ``true`` is what the restore screen
    asks for, and it is the only way back to a removed task.
    """

    model_config = ConfigDict(frozen=True)

    project_code: str
    is_archived: bool = False
    assignee_codes: tuple[str, ...] = ()
    priority_codes: tuple[str, ...] = ()
    state_code: str | None = None
    state_category: str | None = None
    is_overdue: bool | None = None
    search: str = ""
    order_by: str = DEFAULT_TASK_ORDERING
    limit: int = Field(default=50, ge=1, le=200)
    offset: int = Field(default=0, ge=0)


def read_project_tasks(filters: TaskFilters, *, as_of: date) -> Page[TaskView]:
    """Return one window of a project's tasks, plus the total match count.

    Args:
        filters: The requested facets, ordering and window.
        as_of: The date lateness is measured against, for both the ``is_overdue`` facet and the
            ``is_overdue`` field of every returned row.

    Returns:
        The page and how many tasks the filters matched in total.

    Raises:
        ProjectNotFound: No project carries ``filters.project_code``. Raised rather than returning
            an empty page, so a typo in a code is distinguishable from a project with no tasks.
        UnknownOrdering: ``filters.order_by`` names a field outside the allowlist.
    """
    if not Project.objects.by_code(filters.project_code).exists():
        raise ProjectNotFound(filters.project_code)

    matched = _matching(filters, as_of=as_of)
    window = _ordered(matched, order_by=filters.order_by)[
        filters.offset : filters.offset + filters.limit
    ]
    return Page(
        items=window.with_relations().with_dependencies().as_views(today=as_of),
        count=matched.count(),
    )


def _matching(filters: TaskFilters, *, as_of: date) -> TaskQuerySet:
    """Apply the facets. A facet left ``None`` or empty does not filter.

    ``is_archived`` is applied first and always, because it is the scope rather than a facet: every
    other predicate narrows within the set of tasks the caller asked to see.
    """
    scoped = Task.objects.for_project(filters.project_code)
    tasks = scoped.archived() if filters.is_archived else scoped.active()
    if filters.assignee_codes:
        tasks = tasks.filter(assignee__code__in=filters.assignee_codes)
    if filters.priority_codes:
        tasks = tasks.filter(priority__code__in=filters.priority_codes)
    if filters.state_code is not None:
        tasks = tasks.filter(workflow_state__code=filters.state_code)
    if filters.state_category is not None:
        tasks = tasks.in_state_category(filters.state_category)
    if filters.is_overdue is True:
        tasks = tasks.open().overdue(as_of)
    elif filters.is_overdue is False:
        tasks = tasks.exclude(due_date__lt=as_of)
    return tasks.search(filters.search)


def _ordered(tasks: TaskQuerySet, *, order_by: str) -> TaskQuerySet:
    """Apply the requested ordering, or the triage order the endpoint defaults to.

    ``attention`` is not in :data:`TASK_ORDERING` because it is not one column: it is severity
    descending, then soonest due with undated last, then code. Expressing it as an allowlist entry
    would mean the client could ask for ``-attention``, which means nothing.

    Raises:
        UnknownOrdering: ``order_by`` names a field outside the allowlist.
    """
    if order_by == DEFAULT_TASK_ORDERING:
        return tasks.in_attention_order()
    return tasks.order_by(*resolve_ordering(order_by, TASK_ORDERING, tiebreaker="code"))
