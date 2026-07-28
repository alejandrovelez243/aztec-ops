"""Reads of the audit trail.

Two named queries, both rendered by the project detail view: the timeline of one entity, and
the whole decision behind one ``correlation_id``. Each returns materialized
:class:`ActivityEntry` values rather than a lazy queryset, so the query executes here and the
API layer never gains the ability to extend it.
"""

from uuid import UUID

from apps.activity.domain.value_objects import TIMELINE_PAGE_SIZE, ActivityEntry
from apps.activity.models import ActivityRecord


def entity_timeline(
    *, entity_type: str, entity_id: str, limit: int = TIMELINE_PAGE_SIZE
) -> list[ActivityEntry]:
    """Return the most recent facts about one entity, newest first.

    ``entity_id`` is the business code (``PRJ-01``), never a primary key, which is what lets a
    task or blocker record be found without joining into another context. The ordering and the
    predicate match ``activity_entity_recent_idx`` exactly, so the limit stops the scan instead
    of sorting the table.

    Args:
        entity_type: ``project`` | ``task`` | ``blocker``.
        entity_id: Business code of the entity.
        limit: Page size. Defaults to the 50 rows the detail view renders.

    Returns:
        Up to ``limit`` entries ordered by ``occurred_at`` descending. Empty when the entity
        has no recorded history — which for a project that exists is itself a finding, since
        creation always appends a ``CREATED`` record.
    """
    records = ActivityRecord.objects.filter(entity_type=entity_type, entity_id=entity_id).order_by(
        "-occurred_at", "-id"
    )[:limit]
    return [record.to_entry() for record in records]


def project_timeline(*, project_code: str, limit: int = TIMELINE_PAGE_SIZE) -> list[ActivityEntry]:
    """Return the timeline rendered by ``GET /api/v1/projects/{code}/timeline``.

    Named separately from :func:`entity_timeline` because it is the query the product actually
    has, and because the entity type is a constant the caller should not have to spell — a
    caller passing ``"projects"`` would silently get an empty timeline instead of an error.

    Args:
        project_code: ``Project.code``, e.g. ``PRJ-01``.
        limit: Page size.

    Returns:
        The project's facts, newest first.
    """
    return entity_timeline(
        entity_type=ActivityRecord.EntityType.PROJECT,
        entity_id=project_code,
        limit=limit,
    )


def decision_trail(*, correlation_id: UUID) -> list[ActivityEntry]:
    """Return every record written under one ``correlation_id``, oldest first.

    This is what makes a reprioritization defensible: "project B was raised" and "project A was
    lowered to make room" are separate rows, and only this query shows they were one decision.
    Ordering is ascending here, unlike the timeline, because a decision is read as a sequence
    of consequences rather than as a feed.

    Args:
        correlation_id: The id threaded through the use case that produced the records.

    Returns:
        The records of that decision, oldest first. Empty when the id never existed; a single
        entry when the decision touched one entity, which is legitimate and not an error.
    """
    records = ActivityRecord.objects.filter(correlation_id=correlation_id).order_by(
        "occurred_at", "id"
    )
    return [record.to_entry() for record in records]
