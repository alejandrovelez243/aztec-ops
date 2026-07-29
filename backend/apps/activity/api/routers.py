"""Routes of the audit trail: the project timeline and the portfolio-wide feed.

Read-only and append-only at the source: reading who changed what is not itself a change, so this
router calls a read service and nothing here writes. Authenticated like every other route — the
trail names people and clients, and the API's default is that you have to be signed in to see it.
"""

from django.http import HttpRequest
from ninja import Query, Router

from apps.activity.api.schemas import PortfolioTimelineQuery, TimelineQuery
from apps.activity.domain.value_objects import ActivityEntry
from apps.activity.services import (
    PortfolioTimelineFilters,
    TimelineFilters,
    read_portfolio_timeline,
    read_project_timeline,
)
from apps.shared.pagination import Page, PageWindow

router = Router(tags=["activity"])


@router.get(
    "/projects/{project_code}/activity",
    response=Page[ActivityEntry],
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


@router.get("/activity", response=Page[ActivityEntry], url_name="portfolio_activity")
def get_portfolio_activity(
    request: HttpRequest, filters: Query[PortfolioTimelineQuery]
) -> Page[ActivityEntry]:
    """The whole portfolio's trail, newest first, in the same item type as a project timeline.

    This is the cross-project reading of the same table: what moved today, across every project,
    task and blocker. Items are identical to
    ``GET /api/v1/projects/{code}/activity`` — one component and one generated type render both,
    and ``entity`` is what tells a row's subject apart here.

    Every facet is optional and none is rejected for naming an unknown value. A verb, entity type,
    origin or actor this deployment does not know returns an empty page rather than a 422: those
    vocabularies move by migration and by the accounts table, and a saved filter must not be able
    to break a read-only screen.
    """
    del request
    window = PageWindow.of(page=filters.page, page_size=filters.page_size)
    return read_portfolio_timeline(
        PortfolioTimelineFilters(
            entity_type=filters.entity_type,
            entity_id=filters.entity_id,
            verbs=tuple(filters.verb),
            actor=filters.actor,
            origin=filters.origin,
            since=filters.since,
            until=filters.until,
            correlation_id=filters.correlation_id,
            limit=window.limit,
            offset=window.offset,
        )
    )
