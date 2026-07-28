"""Assemble the facts one project is scored from, reading only other contexts' named queries.

This module is the single place where the prioritization context looks outside itself. Every fact
comes from a queryset method on the model that owns the rows — ``Project.objects``,
``ActivityRecord.objects``, ``Task.objects``, ``Blocker.objects`` — never from a query written
here, so "open task" keeps exactly one definition, owned by the context that owns tasks.

Collecting everything up front is what keeps the engine pure: once this function returns, no
strategy and no specification needs a database, a clock or a setting.

``accounts`` is deliberately absent from that list. The owner is an ``accounts.User`` and carries
``weekly_capacity_points``, so the numerator of owner load looks like something this module could
assemble itself from the user row plus a task query. It is not: "how loaded is this person" is a
portfolio question whose numerator comes from ``work.Task``, and it keeps its one definition in
``portfolio.repositories``. Assembling it here would put a second answer next to the first.
"""

from datetime import datetime

from django.conf import settings
from pydantic import BaseModel, ConfigDict

from apps.activity.models import ActivityRecord
from apps.portfolio.models import Project
from apps.portfolio.repositories import owner_load_for_codes
from apps.work.models import Blocker, Task

from ..domain.errors import ProjectNotFound
from ..domain.types import SignalInput

#: ``WorkflowState.category`` for work that is actively moving. Read here — never a state ``code``
#: — so the operation can add a state from the admin without this assembly changing.
IN_PROGRESS_CATEGORY = "IN_PROGRESS"


class ProjectFacts(BaseModel):
    """The assembled input plus the primary key the persistence step needs.

    ``project_id`` travels beside the input rather than inside it because the engine ranks business
    facts, not row identities, and ``SignalInput`` is what the database-free tests construct.
    """

    model_config = ConfigDict(frozen=True)

    project_id: int
    signal_input: SignalInput


def collect_project_facts(*, project_code: str, now: datetime) -> ProjectFacts:
    """Read every fact the engine needs about one project at ``now``.

    Args:
        project_code: Business code, e.g. ``"PRJ-01"``.
        now: The instant the recomputation is made for. Ages, overdue counts and staleness are all
            derived against it, so replaying an old event reproduces the score that was correct
            then instead of inventing a new one.

    Returns:
        The project's numeric id and the frozen ``SignalInput`` built from the other contexts.

    Raises:
        ProjectNotFound: No project carries that code. For a consumer this means an event about a
            deleted aggregate, which retrying will not fix.
    """
    project = Project.objects.with_relations().by_code(project_code).first()
    if project is None:
        raise ProjectNotFound(project_code)

    task_counts = Task.objects.for_project(project.pk).counts(today=now.date())
    blockers = Blocker.objects.for_project(project.pk).open().summary()
    last_activity_at = _last_activity_at(project.code)
    owner_load, owner_capacity = _owner_load(project)

    signal_input = SignalInput(
        project_code=project.code,
        now=now,
        target_date=project.target_date,
        is_archived=project.is_archived,
        state_category=project.workflow_state.category,
        next_step=project.next_step,
        has_in_progress_task=_has_task_in_progress(project.pk),
        open_task_count=task_counts.open_task_count,
        overdue_task_count=task_counts.overdue_task_count,
        urgent_open_task_count=task_counts.urgent_open_task_count,
        blocked_task_count=task_counts.blocked_task_count,
        open_blocker_count=blockers.open_blocker_count,
        oldest_blocker_age_days=_age_in_days(blockers.oldest_raised_at, now),
        business_value=project.business_value,
        currency=project.currency,
        last_activity_at=last_activity_at,
        days_since_last_activity=_age_in_days(last_activity_at, now),
        staleness_threshold_days=settings.STALENESS_THRESHOLD_DAYS,
        owner_code=project.owner.code if project.owner is not None else "",
        owner_load_points=owner_load,
        owner_capacity_points=owner_capacity,
        engagement_type_code=project.engagement_type.code,
        engagement_type_weight=project.engagement_type.weight,
    )
    return ProjectFacts(project_id=project.pk, signal_input=signal_input)


def _owner_load(project: Project) -> tuple[int, int]:
    """Current load and capacity of the project's owner, or zeros when it has none.

    An unowned project is a real state in this portfolio, so the absence is handled rather than
    cast away — ``OwnerOverloaded`` then reads an empty owner code and stays silent instead of
    comparing two zeros and flagging everyone.

    The load comes from ``portfolio``'s repository even though the owner is an ``accounts.User``
    whose ``weekly_capacity_points`` is right here: the denominator describes the person, the
    numerator is a ``work.Task`` aggregation, and only ``portfolio`` is allowed to know both. The
    capacity is read off the owner only on the fallback path, where the repository returned no
    row at all because the person has no tasks — a person with no work has a real capacity and a
    load of zero, not an unknown one.
    """
    owner = project.owner
    if owner is None:
        return 0, 0
    load = owner_load_for_codes([owner.code]).get(owner.code)
    if load is None:
        return 0, int(owner.weekly_capacity_points)
    return load.load_points, load.weekly_capacity_points


def _has_task_in_progress(project_id: int) -> bool:
    """Whether any task of the project sits in the ``IN_PROGRESS`` category.

    Composed from ``work``'s own vocabulary rather than from a filter written here: the counts
    aggregate answers four other questions but not this one, and spelling the join out locally
    would put a second definition of "in progress" outside the context that owns tasks. The
    existence check stays in the database — the previous listing loaded every task of the project
    to look at the first one that matched.
    """
    return Task.objects.for_project(project_id).in_state_category(IN_PROGRESS_CATEGORY).exists()


def _last_activity_at(project_code: str) -> datetime | None:
    """When the audit trail last recorded anything about this project.

    ``None`` means nothing has ever been recorded, which ``IsStale`` treats as longer than any
    threshold rather than as freshness.
    """
    timeline = (
        ActivityRecord.objects.for_project(project_code).newest_first().recent(1).as_entries()
    )
    if not timeline:
        return None
    return timeline[0].occurred_at


def _age_in_days(moment: datetime | None, now: datetime) -> int | None:
    """Whole days between ``moment`` and ``now``, or ``None`` when the moment never happened.

    Negative ages are clamped to zero: a blocker raised in the future is a data problem, not a
    reason for a signal to read below its floor.
    """
    if moment is None:
        return None
    return max(0, (now - moment).days)
