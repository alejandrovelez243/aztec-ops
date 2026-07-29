"""Request schemas of the timeline endpoints: one per read, not one with an optional project.

The two reads render different screens and accept different facets, so they declare different
query models (BACKEND §4). A single schema with every field optional would let the portfolio feed
advertise a ``project_code`` the project route already takes from the path.
"""

from datetime import datetime
from uuid import UUID

from ninja import Field, Schema

from apps.shared.pagination import DEFAULT_PAGE_SIZE


class TimelineQuery(Schema):
    """Query parameters of ``GET /api/v1/projects/{code}/activity``.

    There is no ``order_by``: the timeline is a narrative and is always newest first. An allowlist
    with one entry would only advertise a choice that does not exist.

    ``verb`` is repeatable and ORs its values. An unrecognised verb is not rejected — the verb set
    is closed and versioned by migration, so filtering on one this deployment does not know is a
    well-formed question whose answer is "nothing", and a 422 there would break a frontend the
    moment a verb is retired.

    ``correlation_id`` is the interesting one: records sharing it are **one decision**, so passing
    the id from any row expands the whole movement — "A was deprioritized so B could rise" — which
    is what makes a reprioritization defensible weeks later.
    """

    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=DEFAULT_PAGE_SIZE, ge=1)
    verb: list[str] = Field(default_factory=list)
    since: datetime | None = None
    until: datetime | None = None
    correlation_id: UUID | None = None


class PortfolioTimelineQuery(Schema):
    """Query parameters of ``GET /api/v1/activity``, the portfolio-wide feed.

    Every facet is optional and no facet is validated against its vocabulary. ``entity_type``,
    ``verb``, ``origin`` and ``actor`` are declared as plain strings rather than enums on purpose:
    an enum here would turn a retired verb, a renamed origin or a departed colleague's alias —
    values that live in a saved URL or a bookmarked filter — into a 422 on a screen with no bad
    input on it. Filtering on a value this deployment does not know is a well-formed question
    whose answer is "nothing", and that is what the endpoint answers.

    ``entity_id`` is the business code (``PRJ-22``, never a primary key) and is meant to be paired
    with ``entity_type``; alone it still narrows, because codes are unique across kinds in
    practice and the caller passing one read it off a record.

    Like the project timeline there is no ``order_by``: the feed is a narrative and is always
    newest first.
    """

    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=DEFAULT_PAGE_SIZE, ge=1)
    entity_type: str | None = None
    entity_id: str | None = None
    verb: list[str] = Field(default_factory=list)
    actor: str | None = None
    origin: str | None = None
    since: datetime | None = None
    until: datetime | None = None
    correlation_id: UUID | None = None
