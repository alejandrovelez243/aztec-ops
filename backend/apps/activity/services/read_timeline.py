"""Use case: read one page of a project's audit trail.

Ordered ``-occurred_at`` only, and that is not a limitation to be lifted later: the trail is a
narrative, and a narrative sorted by anything else is a list. The ``correlation_id`` facet is what
makes it defensible — "deprioritize A in order to prioritize B" is two rows sharing one id, and
filtering on it reconstructs the whole decision from either half.

Append-only end to end: there is no update and no delete on this surface, which is why the service
module holds a single read.
"""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from apps.activity.domain.value_objects import ActivityEntry
from apps.activity.models import ActivityRecord
from apps.shared.pagination import Page


class TimelineFilters(BaseModel):
    """The facets and window of one timeline read.

    ``verbs`` ORs its values: an operator asking for state changes *and* blocker events wants both
    kinds of row, not the empty intersection.
    """

    model_config = ConfigDict(frozen=True)

    project_code: str
    verbs: tuple[str, ...] = ()
    since: datetime | None = None
    until: datetime | None = None
    correlation_id: UUID | None = None
    limit: int = Field(default=50, ge=1, le=200)
    offset: int = Field(default=0, ge=0)


def read_project_timeline(filters: TimelineFilters) -> Page[ActivityEntry]:
    """Return one window of a project's timeline, plus the total match count.

    An unknown verb is not rejected. The verb set is closed and versioned by migration, so a client
    filtering on one this deployment does not know is asking a well-formed question whose answer is
    "nothing" — and a 422 there would break a frontend the moment a verb is retired.

    A project code matching no project returns an empty page rather than a 404: the trail is keyed
    by business code and outlives the row it describes, so "no records" is the honest answer even
    for a project that once existed.

    Args:
        filters: The requested facets and window.

    Returns:
        The page and how many records the filters matched in total.
    """
    matched = ActivityRecord.objects.for_project(filters.project_code)
    if filters.verbs:
        matched = matched.filter(verb__in=filters.verbs)
    if filters.since is not None:
        matched = matched.filter(occurred_at__gte=filters.since)
    if filters.until is not None:
        matched = matched.filter(occurred_at__lte=filters.until)
    if filters.correlation_id is not None:
        matched = matched.for_correlation(filters.correlation_id)

    window = matched.newest_first()[filters.offset : filters.offset + filters.limit]
    return Page(items=tuple(window.as_entries()), count=matched.count())
