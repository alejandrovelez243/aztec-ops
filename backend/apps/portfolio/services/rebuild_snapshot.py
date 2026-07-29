"""Use case: rebuild one ``ProjectSnapshot`` row from the write side.

This is the read model's only writer, called by the ``snapshot-builder`` handler. It exists because
the command center's question — project + score + owner load + task counts + blockers, ranked — is
a six-table join with per-row aggregates, and resolving that on every request does not hold up
(ARCHITECTURE §8). Paying for it once per event instead of once per page load is the whole trade.

**It projects facts, never conclusions.** Risk flags and health are deliberately not written here
and have no columns to write to (ADR 0011): they are pure functions of the counts, dates and
categories this does store, so they are evaluated when the queue is read. Persisting them would
have bought nothing and cost invalidation — a project whose target date passes at midnight becomes
overdue with no event arriving to rebuild anything.

The row is replaced **wholesale**, never patched. A partial rebuild would leave columns from two
different deliveries in one row while ``last_event_id`` claimed a single one, which is exactly the
kind of staleness that cannot be diagnosed afterwards.

Every fact is read through the owning model's published named queries — ``Task.objects``,
``Blocker.objects``, ``PriorityScore.objects``, ``ActivityRecord.objects`` — so "open task" and
"open blocker" keep exactly one definition each, owned by the context that owns the rows.
"""

from datetime import datetime
from decimal import Decimal
from typing import Final
from uuid import UUID

from django.db import transaction

from apps.activity.models import ActivityRecord
from apps.portfolio.domain.errors import ProjectNotFound
from apps.portfolio.domain.value_objects import ProjectSnapshotValues
from apps.portfolio.models import Project, ProjectSnapshot
from apps.portfolio.repositories import owner_load_for_codes
from apps.prioritization.models import PriorityOverride, PriorityScore
from apps.work.models import Blocker, Task

#: Score of a project the engine has not ranked yet. Zero rather than null, so the queue's
#: ``ORDER BY priority_score DESC`` never has to decide where nulls sort.
_UNSCORED: Final[Decimal] = Decimal("0")


@transaction.atomic
def rebuild_snapshot(
    *, project_code: str, now: datetime, last_event_id: UUID | None = None
) -> ProjectSnapshotValues:
    """Project one project's current state into its read-model row.

    Args:
        project_code: Business code, e.g. ``"PRJ-01"``.
        now: Domain time of the event being applied. The overdue count and the blocker age are
            measured against it rather than against the wall clock, so a redelivery rebuilds the
            same row instead of one that quietly disagrees with the score computed from the same
            event. It does not fix the row's *reading*: staleness and overdue are re-derived from
            these columns whenever the queue is read, against the reader's own clock.
        last_event_id: Envelope id of the event that caused the rebuild, so a stale row is
            traceable to the exact delivery that produced it. ``None`` when rebuilding outside the
            stream, such as from a management command.

    Returns:
        The projection that was written, so a caller can assert on it without re-reading the row.

    Raises:
        ProjectNotFound: The code names no project. For a consumer this is an event about a
            deleted aggregate, which retrying will not fix.
    """
    project = Project.objects.with_relations().by_code(project_code).first()
    if project is None:
        raise ProjectNotFound(project_code)

    counts = Task.objects.for_project(project.pk).counts(today=now.date())
    blockers = Blocker.objects.for_project(project.pk).open().summary()
    score = PriorityScore.objects.for_project(project.pk).first()
    override = _live_override(project_id=project.pk, now=now)
    owner_code = project.owner.code if project.owner is not None else ""
    load_points, capacity_points = _owner_load(owner_code)

    values = ProjectSnapshotValues(
        project_code=project.code,
        project_id=project.pk,
        name=project.name,
        client_alias=project.client.alias,
        engagement_type_code=project.engagement_type.code,
        engagement_type_label=project.engagement_type.label,
        project_type_code=project.project_type.code if project.project_type else "",
        stage_code=project.stage.code if project.stage else "",
        state_code=project.workflow_state.code,
        state_label=project.workflow_state.label,
        state_category=project.workflow_state.category,
        owner_code=owner_code,
        owner_alias=project.owner.alias if project.owner is not None else "",
        owner_load_points=load_points,
        owner_capacity_points=capacity_points,
        start_date=project.start_date,
        target_date=project.target_date,
        business_value=project.business_value,
        currency=project.currency.code,
        next_step=project.next_step,
        priority_score=score.value if score is not None else _UNSCORED,
        priority_policy_version=score.policy_version if score is not None else "",
        breakdown=_breakdown_of(score),
        has_override=override is not None,
        override_position=override.position if override is not None else None,
        override_reason=override.reason if override is not None else "",
        open_task_count=counts.open_task_count,
        overdue_task_count=counts.overdue_task_count,
        blocked_task_count=counts.blocked_task_count,
        urgent_open_task_count=counts.urgent_open_task_count,
        in_progress_task_count=counts.in_progress_task_count,
        open_blocker_count=blockers.open_blocker_count,
        oldest_blocker_age_days=_age_in_days(blockers.oldest_raised_at, now),
        last_activity_at=_last_activity_at(project.code),
        is_archived=project.is_archived,
    )
    ProjectSnapshot.objects.upsert(values, last_event_id)
    return values


def _breakdown_of(score: PriorityScore | None) -> dict[str, object]:
    """The persisted breakdown document, verbatim, or an empty one before the first computation.

    Copied rather than reshaped: the document is owned and validated by the prioritization context
    (DATA_MODEL §6.3), and a read model that rewrote it would be a second opinion about why a
    project ranks where it does.
    """
    if score is None:
        return {}
    document = score.breakdown
    if not isinstance(document, dict):
        return {}
    return dict(document)


def _live_override(*, project_id: int, now: datetime) -> PriorityOverride | None:
    """The override currently forcing this project's position, expiry evaluated against ``now``.

    Expiry is applied here rather than in the named query, because a query must not read a clock
    its caller did not choose — a rebuild replaying an old event would otherwise resolve the
    override against today.
    """
    override = PriorityOverride.objects.for_project(project_id).live().first()
    if override is None:
        return None
    if override.expires_at is not None and override.expires_at <= now:
        return None
    return override


def _owner_load(owner_code: str) -> tuple[int, int]:
    """Load and capacity of the project's owner, or zeros when the project has none.

    An unowned project is a real state in this portfolio, so the absence is handled rather than
    cast away. The load itself comes from ``portfolio.repositories`` — the one query that spans
    contexts — so the snapshot and the ``OwnerOverloaded`` specification cannot disagree about how
    loaded a person is.
    """
    if not owner_code:
        return 0, 0
    load = owner_load_for_codes([owner_code]).get(owner_code)
    if load is None:
        return 0, 0
    return load.load_points, load.weekly_capacity_points


def _last_activity_at(project_code: str) -> datetime | None:
    """When a person last did something about this project, or ``None`` if nobody ever has.

    The same predicate the ``staleness`` signal uses, so the "last activity" the board prints
    cannot contradict the reason printed beside the score. The engine's own recomputation records
    are not activity — a column that said "active 2 minutes ago" about a project nobody has
    touched in a month would be worse than empty.
    """
    timeline = (
        ActivityRecord.objects.for_project(project_code)
        .caused_by_people()
        .newest_first()
        .recent(1)
        .as_entries()
    )
    if not timeline:
        return None
    return timeline[0].occurred_at


def _age_in_days(moment: datetime | None, now: datetime) -> int | None:
    """Whole days between ``moment`` and ``now``, or ``None`` when the moment never happened.

    Negative ages are clamped to zero: a blocker raised in the future is a data problem, not a
    reason for the panel to render a negative age.
    """
    if moment is None:
        return None
    return max(0, (now - moment).days)
