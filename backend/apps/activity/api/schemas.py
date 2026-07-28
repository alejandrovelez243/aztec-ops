"""Request schema of the timeline endpoint."""

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
