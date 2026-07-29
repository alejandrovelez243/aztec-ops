"""Immutable values that cross the portfolio boundary.

Every structured value entering or leaving a service is a frozen ``pydantic.BaseModel``: commands
in, results out, and the snapshot projection down into the repository. Pydantic rather than a
record type because it is the same type system django-ninja uses, so an API schema does not have to
restate these fields, and because the value is validated at construction instead of at the boundary
(PATTERNS_BACKEND §9).

Nothing here names a workflow state, a state category or an event topic: those belong to the
contexts that own them (``apps.workflow.models.StateCategory``,
``apps.events.domain.envelope``), and copying them into this module would create a second
definition that drifts.
"""

from datetime import date, datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

#: Derived project health, mirroring ``prioritization.domain.types.Health``. A literal rather than
#: an import of that enum because this is the *filter* vocabulary — what a client may ask the queue
#: for — and django-ninja renders a literal as an enumerated query parameter. It is a facet, never
#: a column: no table stores health (ADR 0011).
Health = Literal["HEALTHY", "AT_RISK", "BLOCKED"]

#: The currency a project is assumed to be billed in when the caller names none — every project in
#: the source dataset but two. A code, resolved against ``catalog.Currency`` by the service: the
#: domain names the default, the database owns which currencies exist.
DEFAULT_CURRENCY_CODE = "USD"


class OwnerLoad(BaseModel):
    """What one person is actually carrying, derived from task rows.

    Never persisted beside ``weekly_capacity_points``: the source ``Team`` sheet's counters are a
    stale projection of the same task rows and importing them would let the roster and the work
    disagree. The numerator is recomputed on every read; ``weekly_capacity_points``, a column of
    ``accounts.User``, is the only stored half.

    ``member_code`` is ``accounts.User.code``. The field keeps its name because it names the role
    the code plays here — the person carrying work — not the table it was read from.

    ``load_points`` is one point per open task. That is a deliberate simplification stated here
    rather than hidden in a query: the operation has no per-task estimate, so counting open work is
    the only honest measure available.
    """

    model_config = ConfigDict(frozen=True)

    member_code: str
    open_task_count: int = Field(ge=0)
    blocked_task_count: int = Field(ge=0)
    urgent_open_task_count: int = Field(ge=0)
    #: Open tasks whose due date has already passed. Derived at read time from ``due_date`` against
    #: the caller's date, never from a stored flag: a persisted "overdue" is wrong the morning
    #: after it was written (ARCHITECTURE §10.1).
    overdue_task_count: int = Field(default=0, ge=0)
    #: Unarchived projects this person owns. Owning work is a different load from carrying tasks —
    #: somebody with three open tasks and seven projects is not idle — so the two are reported
    #: separately rather than summed into one number nobody can decompose.
    owned_project_count: int = Field(default=0, ge=0)
    weekly_capacity_points: int = Field(gt=0)

    @property
    def load_points(self) -> int:
        """Return the load numerator, in the same unit as ``weekly_capacity_points``."""
        return self.open_task_count

    @property
    def is_overloaded(self) -> bool:
        """Whether the member is carrying more open work than their weekly capacity.

        This is the fact ``OwnerOverloaded`` flags on. It never lowers a project's score: priority
        belongs to the work, not to who happens to be free (ARCHITECTURE §4.1).
        """
        return self.load_points > self.weekly_capacity_points


class CreateProjectCommand(BaseModel):
    """Input of the create-project use case, addressed entirely by business codes.

    Codes rather than numeric ids because the API, the fixtures, the event envelope and the audit
    trail all speak codes; accepting ids here would make the service the one place that does not.
    A code that resolves to nothing raises a typed ``*NotFound`` rather than an integrity error.

    ``workflow_state`` is absent on purpose: the initial state is resolved from the engagement
    type's ``WorkflowBinding``, never chosen by the caller.
    """

    model_config = ConfigDict(frozen=True)

    #: Left empty to let the service mint the next ``PRJ-NN``, which is what the HTTP API always
    #: does: `docs/API.md` §2.3 does not accept a code from a client, because a caller that
    #: chooses an identifier can collide with one an already-published event names. A fixture or a
    #: migration pins its own code by setting this.
    code: str = Field(default="", max_length=16)
    name: str = Field(min_length=1, max_length=160)
    client_code: str = Field(min_length=1, max_length=32)
    engagement_type_code: str = Field(min_length=1, max_length=32)
    project_type_code: str | None = None
    stage_code: str | None = None
    owner_code: str | None = None
    start_date: date | None = None
    target_date: date | None = None
    business_value: Decimal | None = None
    currency_code: str = Field(default=DEFAULT_CURRENCY_CODE, min_length=3, max_length=3)
    summary: str = ""
    #: Long-form Markdown, unbounded because the column is a ``TextField``. The empty default is
    #: what a project created without one carries; there is no null description.
    description: str = ""
    next_step: str = Field(default="", max_length=255)
    imported_health: str = Field(default="", max_length=16)


class UpdateProjectCommand(BaseModel):
    """Input of the update-project use case, with absent-versus-null semantics.

    Every field defaults to ``None``, so ``None`` alone cannot mean "clear this". What separates the
    two is ``model_fields_set``: a field the caller did not send is left untouched, a field sent as
    ``null`` is cleared. That distinction is the whole reason this is a model and not a ``**kwargs``
    dict — with a dict, "unset the target date" and "do not touch the target date" are the same
    call, and a portfolio silently loses its deadlines.

    ``workflow_state`` is not updatable here at all; ``transition_project`` is the only writer.
    """

    model_config = ConfigDict(frozen=True)

    code: str = Field(min_length=1, max_length=16)
    name: str | None = Field(default=None, max_length=160)
    client_code: str | None = None
    engagement_type_code: str | None = None
    project_type_code: str | None = None
    stage_code: str | None = None
    owner_code: str | None = None
    start_date: date | None = None
    target_date: date | None = None
    business_value: Decimal | None = None
    currency_code: str | None = Field(default=None, min_length=3, max_length=3)
    summary: str | None = None
    #: Long-form Markdown. Not one of the clearable fields: the column is NOT NULL with an empty
    #: default, so an explicit ``null`` is not an instruction the domain can honour — clearing a
    #: description is sending ``""``.
    description: str | None = None
    next_step: str | None = Field(default=None, max_length=255)
    is_archived: bool | None = None

    def was_provided(self, field_name: str) -> bool:
        """Whether the caller explicitly sent this field, including sending it as ``null``.

        Args:
            field_name: Attribute name on this command.

        Returns:
            True when the field was present in the payload the command was built from.
        """
        return field_name in self.model_fields_set


class ProjectResult(BaseModel):
    """What a portfolio write use case returns to its caller.

    A projection of the aggregate, not the Django instance: a service returning a model would let
    the router lazily trigger queries after the transaction closed, and would tie the HTTP response
    shape to the column list. Nothing here is nullable-by-accident — a null ``target_date`` or
    ``owner_code`` is the operational signal itself.
    """

    model_config = ConfigDict(frozen=True)

    code: str
    name: str
    client_code: str
    client_alias: str
    engagement_type_code: str
    project_type_code: str | None
    stage_code: str | None
    state_code: str
    state_label: str
    state_category: str
    owner_code: str | None
    start_date: date | None
    target_date: date | None
    business_value: Decimal | None
    currency: str
    summary: str
    next_step: str
    is_archived: bool


class ProjectStateChange(BaseModel):
    """Result of a state transition: the new aggregate plus the edge that was traversed.

    The from/to pair is returned rather than re-derived, because after the transition the previous
    state is only recoverable from the audit trail, and the caller needs it to render "moved from X
    to Y" without a second query.
    """

    model_config = ConfigDict(frozen=True)

    project: ProjectResult
    from_state_code: str
    to_state_code: str
    reason: str


#: The signed field names ``GET /api/v1/queue`` accepts, mapped to the snapshot columns they sort.
#: An allowlist rather than a passthrough: ``order_by`` reaches a database, so accepting an
#: arbitrary name is accepting an arbitrary join, and a name outside this mapping is a 422
#: (`docs/API.md` §1.4). ``updated_at`` is the wire's name for ``rebuilt_at`` — the read model has
#: no other "last touched" column, and the projection is rebuilt by every event about the project.
QUEUE_ORDERING: dict[str, str] = {
    "score": "priority_score",
    "target_date": "target_date",
    "name": "name",
    "updated_at": "rebuilt_at",
}

#: The order applied when the caller names none: the queue is a ranking, so it defaults to one.
DEFAULT_QUEUE_ORDERING = "-score"


class SnapshotQueueFilters(BaseModel):
    """The facets and window of one command-center queue read.

    Bundled into a value object rather than passed as fifteen keyword arguments so the queryset
    signature does not grow every time the UI gains a facet, and so an unfiltered read is spelled
    ``SnapshotQueueFilters()`` instead of a wall of ``None`` literals.

    ``None`` means "do not filter". A repeatable facet arrives as a tuple and ORs its values, which
    is what the UI's multi-select produces; ``risk_flag_codes`` is the exception and ANDs, because
    "blocked **and** overdue" is the question an operator asks and "blocked or overdue" is almost
    every project.

    ``is_archived`` defaults to ``False`` rather than to ``None``: archived projects are out of the
    operation's attention by definition, so they are excluded unless explicitly asked for.
    """

    model_config = ConfigDict(frozen=True)

    health: Health | None = None
    state_category: str | None = None
    state_code: str | None = None
    engagement_type_codes: tuple[str, ...] = ()
    project_type_code: str | None = None
    stage_code: str | None = None
    owner_codes: tuple[str, ...] = ()
    risk_flag_codes: tuple[str, ...] = ()
    has_open_blockers: bool | None = None
    is_archived: bool = False
    search: str = ""
    order_by: str = DEFAULT_QUEUE_ORDERING
    limit: int = Field(default=25, ge=1, le=200)
    offset: int = Field(default=0, ge=0)


#: Which half of the roster a read is about. A three-valued enumeration rather than the boolean
#: ``include_inactive`` it replaces, because that flag could not express "show me only the people
#: we have retired" — the question an operator asks before restoring somebody — and a second
#: boolean beside it would make ``include_inactive=False, only_inactive=True`` representable and
#: meaningless (CLAUDE.md rule 13).
RosterStatus = Literal["active", "inactive", "all"]

#: The default: retired people are history, not a staffing constraint, so they are out unless asked
#: for by name.
DEFAULT_ROSTER_STATUS: RosterStatus = "active"

#: The signed field names ``GET /api/v1/team/load`` accepts, mapped to the
#: :class:`~apps.portfolio.domain.views.TeamLoadView` attribute each one sorts.
#:
#: An allowlist for the same reason the queue has one, minus the injection half: this ordering is
#: applied in Python, because half these fields — every count, and ``utilization`` itself — are
#: derived after the query rather than stored in a column the database could sort. Sorting a roster
#: of a few dozen rows in memory costs nothing; storing the counts so the database could sort them
#: would reintroduce the stale projection the source ``Team`` sheet already is.
ROSTER_ORDERING: dict[str, str] = {
    "label": "label",
    "role": "role",
    "utilization": "utilization",
    "load_points": "load_points",
    "capacity": "weekly_capacity_points",
    "open_tasks": "open_tasks",
    "overdue_tasks": "overdue_tasks",
    "blocked_tasks": "blocked_tasks",
    "urgent_tasks": "high_or_critical_open",
    "projects_owned": "projects_owned",
}

#: The order applied when the caller names none. The roster is read to find who is drowning, so it
#: opens on the most loaded person rather than on the alphabet.
DEFAULT_ROSTER_ORDERING = "-utilization"


class RosterFilters(BaseModel):
    """The facets and ordering of one roster read.

    Bundled rather than passed as six keyword arguments, for the same reason
    :class:`SnapshotQueueFilters` is: the read gains facets as the screen does, and a value object
    absorbs that without every caller's signature changing.

    Unset means "do not filter". ``overloaded`` is tri-state on purpose — ``None`` is everybody,
    ``True`` is the people over capacity, ``False`` is the people with room — because a plain
    boolean could not express the third question, and "who has room to take this?" is the reason
    somebody opens the roster while assigning work.

    Not paginated, deliberately. A pager over a roster costs the client a second request to learn
    there is no second page, and this is one of the few reads in the product whose result set is
    bounded by how many people the company employs.
    """

    model_config = ConfigDict(frozen=True)

    owner_codes: tuple[str, ...] = ()
    role_codes: tuple[str, ...] = ()
    status: RosterStatus = DEFAULT_ROSTER_STATUS
    overloaded: bool | None = None
    search: str = ""
    order_by: str = DEFAULT_ROSTER_ORDERING


class ProjectSnapshotValues(BaseModel):
    """The full projection written into one ``ProjectSnapshot`` row.

    Produced by the ``snapshot-builder`` handler from ``Project``, ``Task``, ``Blocker``,
    ``PriorityScore`` and ``ActivityRecord``, then handed to
    :meth:`apps.portfolio.models.ProjectSnapshotQuerySet.upsert`. It is a complete row, never
    a patch: a partial rebuild would leave columns from two different deliveries in one row, and
    ``last_event_id`` would then name a delivery that did not produce all of it.

    ``breakdown`` is typed loosely because it is a verbatim copy of ``PriorityScore.breakdown``
    (DATA_MODEL §6.3), owned and validated by the prioritization context.

    It carries **no flags and no health**, and that is the point of ADR 0011. What it does carry is
    what those two are derived *from* — the state category, the dates, the next step, the task and
    blocker counts, the owner's load and the last activity — because those are genuinely expensive
    to aggregate per request and are facts rather than conclusions. The conclusion is drawn at read
    time, so a row rebuilt a week ago still reads as overdue the morning the date passes.
    """

    model_config = ConfigDict(frozen=True)

    project_code: str
    project_id: int
    name: str
    client_alias: str
    engagement_type_code: str
    engagement_type_label: str
    project_type_code: str = ""
    stage_code: str = ""
    state_code: str
    state_label: str
    state_category: str
    owner_code: str = ""
    owner_alias: str = ""
    owner_load_points: int = 0
    owner_capacity_points: int = 0
    start_date: date | None = None
    target_date: date | None = None
    business_value: Decimal | None = None
    currency: str = "USD"
    next_step: str = ""
    priority_score: Decimal = Decimal("0")
    priority_policy_version: str = ""
    breakdown: dict[str, object] = Field(default_factory=dict)
    has_override: bool = False
    override_position: int | None = None
    override_reason: str = ""
    open_task_count: int = 0
    overdue_task_count: int = 0
    blocked_task_count: int = 0
    urgent_open_task_count: int = 0
    in_progress_task_count: int = 0
    open_blocker_count: int = 0
    oldest_blocker_age_days: int | None = None
    last_activity_at: datetime | None = None
    is_archived: bool = False
