"""Route of the audit trail: the project timeline.

Read-only, unauthenticated and append-only at the source. No ``X-Actor`` is required, because
reading who changed what is not itself a change.
"""

from django.http import HttpRequest
from ninja import Query, Router

from apps.activity.api.schemas import TimelineQuery
from apps.activity.domain.value_objects import ActivityEntry
from apps.activity.services import TimelineFilters, read_project_timeline
from apps.shared.pagination import Page, PageWindow

router = Router(tags=["activity"])


@router.get(
    "/projects/{project_code}/activity",
    response=Page[ActivityEntry],
    auth=None,
    url_name="project_activity",
)
def get_project_activity(
    request: HttpRequest, project_code: str, filters: Query[TimelineQuery]
) -> Page[ActivityEntry]:
    """The project's timeline, newest first.

    ``metadata.origin`` distinguishes ``MANUAL`` from ``POLICY`` on every ``PRIORITY_CHANGED`` row,
    which is how a reader tells a human's decision from the engine's recomputation. Records sharing
    a ``correlation_id`` are one decision and the UI groups them.

    A project code matching nothing returns an empty page rather than a 404: the trail is keyed by
    business code and outlives the row it describes, so "no records" is the honest answer even for
    a project that no longer exists.
    """
    del request
    window = PageWindow.of(page=filters.page, page_size=filters.page_size)
    return read_project_timeline(
        TimelineFilters(
            project_code=project_code,
            verbs=tuple(filters.verb),
            since=filters.since,
            until=filters.until,
            correlation_id=filters.correlation_id,
            limit=window.limit,
            offset=window.offset,
        )
    )
