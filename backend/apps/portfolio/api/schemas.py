"""Request schemas of the portfolio endpoints — one per use case, never one for all of them.

A single ``ProjectSchema`` with every field optional would type-check a request that sets no field
at all, and would let ``POST`` accept ``workflow_state`` because ``PATCH`` needed something near
it. Each schema below is exactly the payload one route accepts, which is what makes "``code`` is
not accepted here" a compile-time fact rather than a runtime check.

Two fields are rejected everywhere on purpose. ``workflow_state`` is not writable — a state moves
through ``POST /projects/{code}/transition`` and nowhere else (CLAUDE.md rule 2). ``health`` and
``score`` are derived, so accepting them would offer a caller a second, wrong answer to a question
the system already answers.

Every value is a taxonomy ``code``, never a Spanish label: labels are operator-editable data and a
payload comparing against one breaks the first time somebody fixes a typo in the admin (rule 1).
"""

from datetime import date
from decimal import Decimal

from ninja import Field, Schema

from apps.portfolio.domain.value_objects import (
    DEFAULT_CURRENCY_CODE,
    DEFAULT_QUEUE_ORDERING,
    DEFAULT_ROSTER_ORDERING,
    DEFAULT_ROSTER_STATUS,
    Health,
    RosterStatus,
)
from apps.shared.pagination import DEFAULT_PAGE_SIZE


class ProjectCreateIn(Schema):
    """Body of ``POST /api/v1/projects``.

    ``code`` is absent by design: the service allocates the next ``PRJ-NN`` inside the creating
    transaction. A client that chose its own could collide with a code an already-published event
    names, and the event cannot be un-published.

    ``workflow_state`` is absent for the same reason it is absent from the update schema: the
    project starts in the ``is_initial`` state of the workflow its engagement type binds to, so a
    caller cannot enter a project directly into a state no declared transition leads to.
    """

    name: str = Field(min_length=1, max_length=200)
    client: str = Field(min_length=1, max_length=32)
    engagement_type: str = Field(min_length=1, max_length=32)
    project_type: str | None = None
    stage: str | None = None
    owner: str | None = None
    start_date: date | None = None
    target_date: date | None = None
    business_value: Decimal | None = Field(default=None, ge=0)
    currency: str = Field(default=DEFAULT_CURRENCY_CODE, min_length=3, max_length=3)
    summary: str = ""
    next_step: str = Field(default="", max_length=255)


class ProjectUpdateIn(Schema):
    """Body of ``PATCH /api/v1/projects/{code}``.

    Every field is optional, and "absent" and "``null``" mean different things: an absent field is
    left untouched, an explicitly ``null`` one clears a nullable column. That distinction is the
    whole reason this is a schema and not a dict — with a dict, "unset the target date" and "do
    not touch the target date" are the same request, and a portfolio quietly loses its deadlines.

    The router reads which fields were actually sent from Pydantic's ``model_fields_set`` and
    carries that set into the command, so the distinction survives all the way to the service.
    """

    name: str | None = Field(default=None, min_length=1, max_length=200)
    client: str | None = Field(default=None, min_length=1, max_length=32)
    engagement_type: str | None = Field(default=None, min_length=1, max_length=32)
    project_type: str | None = None
    stage: str | None = None
    owner: str | None = None
    start_date: date | None = None
    target_date: date | None = None
    business_value: Decimal | None = Field(default=None, ge=0)
    currency: str | None = Field(default=None, min_length=3, max_length=3)
    summary: str | None = None
    next_step: str | None = Field(default=None, max_length=255)
    is_archived: bool | None = None


class TransitionIn(Schema):
    """Body of ``POST /api/v1/projects/{code}/transition``.

    ``reason`` is optional *here* and mandatory *there*: whether an edge demands one is
    ``WorkflowTransition.requires_reason``, a row an operator edits, so making it required in the
    schema would freeze a data decision into code. The transition service raises
    ``ReasonRequired`` — a typed 422 naming the field — when the edge asks for one and it is blank.
    """

    to_state: str = Field(min_length=1, max_length=32)
    reason: str = ""


# ``NoteIn`` deliberately does not live here. ``POST /projects/{code}/notes`` hangs off a project
# path, but a note is a ``work`` aggregate and its schema belongs to the context that owns the use
# case (``apps.work.api.schemas``). Two schemas for one route is how the two quietly disagree about
# whether ``task_code`` is optional.


class QueueQuery(Schema):
    """Query parameters of ``GET /api/v1/queue``.

    Repeatable facets arrive as lists and OR their values, which is what a multi-select means.
    ``risk_flag`` is the exception and ANDs: "blocked **and** overdue" is the question an operator
    asks, and the OR of two common flags is most of the portfolio.

    An unknown value is not an error — it simply matches nothing — but an unparseable one is a 422,
    and so is an ``order_by`` outside the allowlist (`docs/API.md` §1.4).
    """

    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=DEFAULT_PAGE_SIZE, ge=1)
    owner: list[str] = Field(default_factory=list)
    engagement_type: list[str] = Field(default_factory=list)
    risk_flag: list[str] = Field(default_factory=list)
    project_type: str | None = None
    stage: str | None = None
    state: str | None = None
    state_category: str | None = None
    health: Health | None = None
    has_open_blockers: bool | None = None
    is_archived: bool = False
    q: str = ""
    order_by: str = DEFAULT_QUEUE_ORDERING


class TeamLoadQuery(Schema):
    """Query parameters of ``GET /api/v1/team/load``.

    Not paginated: the roster is bounded by how many people the company employs, and a pager over
    it costs the client a second request to learn there is no second page.

    ``status`` replaced the earlier ``include_inactive`` boolean, which could not express "show me
    only the people we have retired" — the question an operator asks before restoring somebody.

    ``overloaded`` is tri-state: absent is everybody, ``true`` is the people over capacity, and
    ``false`` is the people with room, which is what somebody about to assign work is looking for.
    """

    owner: list[str] = Field(default_factory=list)
    role: list[str] = Field(default_factory=list)
    status: RosterStatus = DEFAULT_ROSTER_STATUS
    overloaded: bool | None = None
    q: str = ""
    order_by: str = DEFAULT_ROSTER_ORDERING
