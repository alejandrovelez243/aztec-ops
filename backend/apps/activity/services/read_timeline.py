"""Use cases: read one page of the audit trail, scoped to a project or to the whole portfolio.

Ordered ``-occurred_at`` only, and that is not a limitation to be lifted later: the trail is a
narrative, and a narrative sorted by anything else is a list. The ``correlation_id`` facet is what
makes it defensible — "deprioritize A in order to prioritize B" is two rows sharing one id, and
filtering on it reconstructs the whole decision from either half.

Two reads and not one, because they answer different questions and must not share a filter type:
the project timeline always knows its project, while the portfolio feed treats the entity as a
facet that may be absent entirely. Folding them into one service with an optional ``project_code``
would make "no project" and "this project" the same call, and the caller could no longer be sure
which one it asked for.

Append-only end to end: there is no update and no delete on this surface, which is why the module
holds reads and nothing else.
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
    matched = (
        ActivityRecord.objects.for_project(filters.project_code)
        .with_verbs(filters.verbs)
        .occurred_between(filters.since, filters.until)
    )
    if filters.correlation_id is not None:
        matched = matched.for_correlation(filters.correlation_id)

    window = matched.newest_first()[filters.offset : filters.offset + filters.limit]
    return Page(items=tuple(window.as_entries()), count=matched.count())


class PortfolioTimelineFilters(BaseModel):
    """The facets and window of one portfolio-wide timeline read.

    Every facet is optional, and that is the point: the unfiltered read is the feed the command
    center opens on, so the empty instance has to be a valid question rather than a degenerate one.

    ``entity_id`` is only meaningful alongside ``entity_type`` — business codes are unique per kind
    (``PRJ-22``, ``TSK-108``), so an id without a type would be answered by scanning every kind for
    a match. It is accepted anyway and simply narrows on the id column, because the caller that
    passes one already knows which entity it read it from.
    """

    model_config = ConfigDict(frozen=True)

    entity_type: str | None = None
    entity_id: str | None = None
    verbs: tuple[str, ...] = ()
    actor: str | None = None
    origin: str | None = None
    since: datetime | None = None
    until: datetime | None = None
    correlation_id: UUID | None = None
    limit: int = Field(default=50, ge=1, le=200)
    offset: int = Field(default=0, ge=0)


def read_portfolio_timeline(filters: PortfolioTimelineFilters) -> Page[ActivityEntry]:
    """Return one window of the whole portfolio's trail, plus the total match count.

    Same contract as :func:`read_project_timeline` one level up: newest first, one page, and no
    ordering choice. The items are the identical :class:`ActivityEntry`, so the feed and the
    per-project timeline render through one component and one generated client type.

    No facet is validated against its vocabulary. An unknown verb, entity type, origin or actor
    yields an empty page rather than a 422 — those vocabularies are versioned by migration and by
    the accounts table, and a filter naming a value this deployment retired is a well-formed
    question whose honest answer is "nothing". The failure mode a 422 would create is worse than
    the one it prevents: a saved filter in someone's URL breaking a screen that has no bad input
    on it.

    Args:
        filters: The requested facets and window. An instance with no facet set is the unfiltered
            feed and is the expected common case.

    Returns:
        The page and how many records the filters matched in total. ``count`` is a second query
        against the same predicate, not the length of ``items``.
    """
    matched = (
        ActivityRecord.objects.about(filters.entity_type, filters.entity_id)
        .with_verbs(filters.verbs)
        .occurred_between(filters.since, filters.until)
    )
    if filters.actor is not None:
        matched = matched.by_actor(filters.actor)
    if filters.origin is not None:
        matched = matched.from_origin(filters.origin)
    if filters.correlation_id is not None:
        matched = matched.for_correlation(filters.correlation_id)

    window = matched.newest_first()[filters.offset : filters.offset + filters.limit]
    return Page(items=tuple(window.as_entries()), count=matched.count())
