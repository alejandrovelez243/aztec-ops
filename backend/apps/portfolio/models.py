"""Persistence for the portfolio context: clients, projects and the read model.

This module holds fields, constraints, indexes, ``__str__`` and the named queries of each table.
Business rules live in ``apps.portfolio.services`` (ARCHITECTURE §7). The one query that is *not*
here is owner load, which aggregates ``work.Task`` and therefore belongs to no single model; it
stays a module-level function in ``apps.portfolio.repositories``. Three invariants are visible here
and nowhere else:

* ``Project.workflow_state`` is written **only** by the transition service. The column is a plain
  FK because a database constraint cannot express "an active WorkflowTransition exists"; the
  service raises ``TransitionNotAllowed`` instead (DATA_MODEL §9.3).
* ``ProjectSnapshot`` carries ``project_id`` as a plain column rather than a foreign key. The read
  side must survive the write side being rebuilt, so it is coupled to the business code only
  (DATA_MODEL §8).
* There is no person model here. A project owner is an ``accounts.User``: the person who is
  assigned work is the person who signs in to move it, so identity and roster are one row rather
  than two joined by a nullable link that is never null. ``weekly_capacity_points`` therefore lives
  on the user, while the query that derives the load numerator from ``work.Task`` stays in
  ``apps.portfolio.repositories`` — "who is overloaded?" is a portfolio question.
"""

from typing import TYPE_CHECKING
from uuid import UUID

from django.conf import settings
from django.contrib.postgres.indexes import GinIndex
from django.db import models

from apps.portfolio.domain.value_objects import QUEUE_ORDERING, ProjectResult
from apps.portfolio.domain.views import (
    HEALTH_LABELS,
    HealthRef,
    QueueItemView,
    QueueOverrideView,
)
from apps.prioritization.domain.views import RiskFlagView, ScoreView
from apps.shared.ordering import resolve_ordering
from apps.shared.refs import ActorRef, StateRef, TaxonomyRef

if TYPE_CHECKING:
    from apps.portfolio.domain.value_objects import ProjectSnapshotValues, SnapshotQueueFilters

#: Prefix and minimum width of a portfolio code (``PRJ-01``). The width is a floor, not a limit:
#: the 100th project is ``PRJ-100`` and stays sortable.
PROJECT_CODE_PREFIX = "PRJ-"
PROJECT_CODE_DIGITS = 2

#: Joins every projection of a project reads. Kept in one place so a caller cannot half-populate a
#: result and then pay for the rest one lazy query at a time.
_PROJECT_RELATIONS = (
    "client",
    "engagement_type",
    "project_type",
    "stage",
    "workflow_state",
    "owner",
    "currency",
)


class ProjectQuerySet(models.QuerySet["Project"]):
    """Named reads and locks of the project aggregate."""

    def with_relations(self) -> "ProjectQuerySet":
        """Load every relation :meth:`Project.to_result` reads.

        The projection is serialized after its transaction closes, so a relation left lazy
        becomes a query outside that transaction. Chained by every read that ends in a
        projection rather than applied automatically, so an existence check stays a single
        index probe.
        """
        return self.select_related(*_PROJECT_RELATIONS)

    def by_code(self, project_code: str) -> "ProjectQuerySet":
        """Narrow to the project carrying this business code.

        Args:
            project_code: ``Project.code``, e.g. ``"PRJ-01"``.
        """
        return self.filter(code=project_code)

    def locked(self) -> "ProjectQuerySet":
        """Row-lock the selection for the duration of the enclosing transaction.

        Every write use case chains this so two concurrent updates serialize instead of
        last-write-wins on a read-modify-write. It requires an open transaction: evaluating the
        queryset outside ``transaction.atomic()`` raises ``TransactionManagementError``.

        Only the project row is locked (``of="self"``). Three of the relations
        :meth:`with_relations` joins are nullable, and PostgreSQL refuses ``FOR UPDATE`` on the
        nullable side of an outer join; locking the taxonomy rows would also serialize every
        other project sharing a stage. The aggregate root is the row a concurrent write would
        corrupt.
        """
        return self.select_for_update(of=("self",))

    def active(self) -> "ProjectQuerySet":
        """Narrow to the projects still in the operation's attention: the unarchived ones."""
        return self.filter(is_archived=False)

    def next_code(self) -> str:
        """Mint the next portfolio code — ``PRJ-23``.

        **Ends the chain**: it materialises the codes already in use. Derived from the highest
        number present rather than from a count, so deleting a project never causes the next one
        to reuse a retired code — and a code that can be reused is a code the audit trail and the
        event bus cannot rely on.

        Archived projects still count. They keep their code forever, and handing it to a new
        project would silently merge two histories in the timeline.

        This is *not* collision-proof under concurrency: two simultaneous creations can read the
        same maximum, and the unique constraint rejects the loser. Acceptable while project
        creation is an operator filling a form; a PostgreSQL sequence is the fix if that changes.

        Returns:
            The next free project code.
        """
        highest = 0
        for code in self.values_list("code", flat=True):
            suffix = str(code).removeprefix(PROJECT_CODE_PREFIX)
            if suffix.isdigit():
                highest = max(highest, int(suffix))
        return f"{PROJECT_CODE_PREFIX}{highest + 1:0{PROJECT_CODE_DIGITS}d}"

    def owned_by(self, owner_code: str) -> "ProjectQuerySet":
        """Narrow to the projects one person owns, named by ``accounts.User.code``.

        Owner load is a property of the *person*, so a task event changes that number on every
        snapshot that person owns and not only on the project the task belongs to. This is the
        selection the snapshot rebuild widens to (DATA_MODEL §8). Unowned projects are excluded by
        construction: nobody carries them, which is a different fact from carrying nothing.

        Args:
            owner_code: ``accounts.User.code``, e.g. ``"daniel.rojas"``.
        """
        return self.filter(owner__code=owner_code)

    def codes(self) -> list[str]:
        """Materialise the selection as business codes, ending the chain.

        Codes because everything downstream of a selection of projects — an event envelope, an
        audit record, a snapshot row — is addressed by ``Project.code``, so returning rows would
        make each caller project them again.
        """
        return [str(code) for code in self.values_list("code", flat=True)]


class ProjectSnapshotQuerySet(models.QuerySet["ProjectSnapshot"]):
    """Named reads and the single write of the command-center read model."""

    def in_attention(self) -> "ProjectSnapshotQuerySet":
        """Narrow to the projects the operation is still paying attention to.

        Archived projects are excluded by every queue read and this is not a parameter: they are
        out of the operation's attention by definition, and making it optional would invite a
        caller to rank them.
        """
        return self.filter(is_archived=False)

    def matching(self, filters: "SnapshotQueueFilters") -> "ProjectSnapshotQuerySet":
        """Apply the command center's facets. A facet left ``None`` or empty does not filter.

        Every value compared here is a taxonomy ``code``, a state ``category`` or a flag ``code`` —
        never an operator-editable label (CLAUDE.md rule 1). ``search`` is the one exception and it
        is a substring match, not an equality test, so a renamed client cannot make a saved filter
        silently match nothing.

        Repeatable facets OR their values, because that is what a multi-select means.
        ``risk_flag_codes`` ANDs instead: "blocked **and** overdue" is the question the panel asks,
        and the OR of two common flags is most of the portfolio.

        Args:
            filters: The requested facets. The window (``offset``/``limit``) and the ordering are
                not applied here — chain :meth:`in_requested_order` and :meth:`page` — so a caller
                can count the whole match before paging it.
        """
        queryset = self.filter(is_archived=filters.is_archived)
        if filters.health is not None:
            queryset = queryset.filter(health=filters.health)
        if filters.state_category is not None:
            queryset = queryset.filter(state_category=filters.state_category)
        if filters.state_code is not None:
            queryset = queryset.filter(state_code=filters.state_code)
        if filters.engagement_type_codes:
            queryset = queryset.filter(engagement_type_code__in=filters.engagement_type_codes)
        if filters.project_type_code is not None:
            queryset = queryset.filter(project_type_code=filters.project_type_code)
        if filters.stage_code is not None:
            queryset = queryset.filter(stage_code=filters.stage_code)
        if filters.owner_codes:
            queryset = queryset.filter(owner_code__in=filters.owner_codes)
        for flag_code in filters.risk_flag_codes:
            # GIN containment, so ?risk_flag=BLOCKED never joins prioritization_riskflag. One
            # predicate per requested flag is what makes the set AND rather than OR.
            queryset = queryset.filter(risk_flags__contains=[{"code": flag_code}])
        if filters.has_open_blockers is True:
            queryset = queryset.filter(open_blocker_count__gt=0)
        elif filters.has_open_blockers is False:
            queryset = queryset.filter(open_blocker_count=0)
        if filters.search.strip():
            term = filters.search.strip()
            queryset = queryset.filter(
                models.Q(project_code__icontains=term)
                | models.Q(name__icontains=term)
                | models.Q(client_alias__icontains=term)
            )
        return queryset

    def in_queue_order(self) -> "ProjectSnapshotQuerySet":
        """Order as the queue: score descending, ties broken by code so paging is stable.

        Matches the ``(is_archived, priority_score DESC)`` index, so combined with
        :meth:`in_attention` the plan carries no sort node.
        """
        return self.order_by("-priority_score", "project_code")

    def in_requested_order(self, order_by: str) -> "ProjectSnapshotQuerySet":
        """Order by one signed field name drawn from the endpoint's allowlist.

        ``project_code`` is always appended as the final key. Without it two rows tying on the
        requested field have no defined relative order, and PostgreSQL is free to return them in
        different orders on two pages of the same scan — which shows up as a row appearing twice
        while another never appears at all.

        Args:
            order_by: A key of ``QUEUE_ORDERING``, optionally prefixed with ``-`` for descending.

        Returns:
            The ordered selection.

        Raises:
            UnknownOrdering: The field is outside the allowlist. Rejected rather than ignored: a
                silently different order is a wrong answer the client cannot detect.
        """
        return self.order_by(*resolve_ordering(order_by, QUEUE_ORDERING, tiebreaker="project_code"))

    def as_queue_items(self) -> tuple["QueueItemView", ...]:
        """Materialise the selection as the rows the command center renders.

        **Ends the chain**: the query executes here, so no caller can narrow the queue after the
        response shape was decided. Every field comes from this one table — that is the whole
        point of the read model — so this issues exactly one query however many rows it returns.
        """
        return tuple(snapshot.to_queue_item() for snapshot in self)

    def page(self, *, offset: int, limit: int) -> "ProjectSnapshotQuerySet":
        """Take one window of the current ordering.

        Ends the chain for filtering: the queryset stays lazy but sliced, so no caller can add a
        predicate after the window was decided.

        Args:
            offset: Rows to skip.
            limit: Rows to take.
        """
        return self[offset : offset + limit]

    def by_code(self, project_code: str) -> "ProjectSnapshotQuerySet":
        """Narrow to one snapshot by business code.

        Args:
            project_code: ``Project.code``, e.g. ``"PRJ-01"``.
        """
        return self.filter(project_code=project_code)

    def upsert(self, values: "ProjectSnapshotValues", last_event_id: UUID | None = None) -> None:
        """Replace one snapshot row from a fully-built projection. Writes; ends the chain.

        Keyed on ``project_code`` rather than a numeric id so the rebuild consumer never has to
        resolve the write side first, which is also what lets the read model survive the write
        side being rebuilt. The row is replaced wholesale: a partial write would mix columns
        from two deliveries while ``last_event_id`` claimed a single one.

        The only permitted caller is the ``snapshot-rebuild`` consumer group. A write path that
        edits a snapshot instead of emitting its event has removed the only thing that keeps the
        row reproducible (PATTERNS §8).

        Args:
            values: The complete projection for this project.
            last_event_id: Envelope id of the event that produced the projection, for tracing a
                stale row back to its delivery. ``None`` only when rebuilding outside the
                bus, such as from the admin's recompute action.
        """
        defaults = values.model_dump(exclude={"project_code", "risk_flags"})
        defaults["risk_flags"] = [flag.model_dump() for flag in values.risk_flags]
        defaults["last_event_id"] = last_event_id
        self.update_or_create(project_code=values.project_code, defaults=defaults)


class Client(models.Model):
    """A counterparty the operation delivers to.

    ``code`` is the stable slug fixtures and code refer to; ``alias`` is operator-editable display
    text and is never compared against.
    """

    code = models.CharField(max_length=32)
    alias = models.CharField(max_length=96)
    notes = models.TextField(blank=True, default="")
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "client"
        verbose_name_plural = "clients"
        ordering = ["alias"]
        constraints = [
            models.UniqueConstraint(fields=["code"], name="portfolio_client_code_unique"),
        ]

    def __str__(self) -> str:
        """Return the display alias."""
        return self.alias


class Project(models.Model):
    """The portfolio aggregate root.

    Nullability here is meaning, not missing data: a null ``target_date`` is the ``NO_TARGET_DATE``
    risk signal and is never backfilled with ``today()`` or a sentinel, and a null ``owner`` is
    itself an operational signal. ``next_step`` is an empty string rather than null so
    ``HasNoNextStep`` stays a single predicate.

    There is no ``status`` column and no writable ``health`` column: status is ``workflow_state``,
    health is derived from open ``RiskFlag`` rows at read time.
    """

    code = models.CharField(max_length=16)
    name = models.CharField(max_length=160)
    client = models.ForeignKey(Client, on_delete=models.PROTECT, related_name="projects")
    engagement_type = models.ForeignKey(
        "catalog.EngagementType",
        on_delete=models.PROTECT,
        related_name="projects",
    )
    project_type = models.ForeignKey(
        "catalog.ProjectType",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="projects",
    )
    stage = models.ForeignKey(
        "catalog.Stage",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="projects",
    )
    workflow_state = models.ForeignKey(
        "workflow.WorkflowState",
        on_delete=models.PROTECT,
        related_name="projects",
    )
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="owned_projects",
    )
    start_date = models.DateField(null=True, blank=True)
    target_date = models.DateField(null=True, blank=True, db_index=True)
    business_value = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    # A taxonomy row, not a three-letter string: the frontend renders a validated select from the
    # served list, and ``minor_units`` on the row is what tells it 28000 CLP is not 28000 USD.
    # PROTECT because a currency is referenced by history — deleting one would rewrite what a
    # signed contract was worth. Retire it with ``is_active`` instead.
    currency = models.ForeignKey(
        "catalog.Currency",
        on_delete=models.PROTECT,
        related_name="projects",
    )
    summary = models.TextField(blank=True, default="")
    next_step = models.CharField(max_length=255, blank=True, default="")
    is_archived = models.BooleanField(default=False)
    imported_health = models.CharField(max_length=16, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = ProjectQuerySet.as_manager()

    class Meta:
        verbose_name = "project"
        verbose_name_plural = "projects"
        ordering = ["code"]
        constraints = [
            models.UniqueConstraint(fields=["code"], name="portfolio_project_code_unique"),
            models.CheckConstraint(
                condition=(
                    models.Q(start_date__isnull=True)
                    | models.Q(target_date__isnull=True)
                    | models.Q(start_date__lte=models.F("target_date"))
                ),
                name="portfolio_project_dates_ordered",
            ),
            models.CheckConstraint(
                condition=models.Q(business_value__isnull=True) | models.Q(business_value__gte=0),
                name="portfolio_project_business_value_non_negative",
            ),
        ]
        indexes = [
            models.Index(
                fields=["is_archived", "workflow_state"],
                name="portfolio_prj_arch_state",
            ),
            models.Index(fields=["owner", "is_archived"], name="portfolio_prj_owner_arch"),
        ]

    def __str__(self) -> str:
        """Return the business code and name, which is how operators refer to a project."""
        return f"{self.code} — {self.name}"

    def to_result(self) -> ProjectResult:
        """Describe this aggregate as the frozen value every write use case returns.

        ``create_project``, ``update_project`` and ``transition_project`` all end here, and what
        they return is serialized by the router after their transaction has closed. Returning the
        instance instead would let the router resolve ``client``, ``stage`` or ``owner`` lazily at
        that point — a query outside the transaction that produced the value — and would tie the
        HTTP response shape to the column list.

        Deliberately omitted: ``pk``, ``created_at``, ``updated_at`` (row bookkeeping no caller
        renders) and ``imported_health``, which is the spreadsheet's opinion of health kept only
        for reconciliation — health is derived from open ``RiskFlag`` rows and is served from
        ``ProjectSnapshot``, so exposing the imported column here would offer callers a second,
        stale answer to the same question.

        The seven relations read below must already be loaded: ``ProjectQuerySet.with_relations``
        selects every one of them. On an instance fetched without them this is still correct but each attribute
        triggers its own lazy query, which is exactly the escape from the transaction the
        projection exists to prevent.

        Returns:
            The aggregate as an immutable :class:`ProjectResult`, safe to serialize outside the
            transaction.
        """
        return ProjectResult(
            code=self.code,
            name=self.name,
            client_code=self.client.code,
            client_alias=self.client.alias,
            engagement_type_code=self.engagement_type.code,
            project_type_code=self.project_type.code if self.project_type else None,
            stage_code=self.stage.code if self.stage else None,
            state_code=self.workflow_state.code,
            state_label=self.workflow_state.label,
            state_category=self.workflow_state.category,
            owner_code=self.owner.code if self.owner else None,
            start_date=self.start_date,
            target_date=self.target_date,
            business_value=self.business_value,
            currency=self.currency.code,
            summary=self.summary,
            next_step=self.next_step,
            is_archived=self.is_archived,
        )


class ProjectSnapshot(models.Model):
    """Denormalized read model backing the command center (ARCHITECTURE §8, DATA_MODEL §3).

    One row per project, pre-joined so ``GET /api/v1/projects`` is a single index scan on
    ``(is_archived, priority_score DESC)`` instead of a six-table join with per-row aggregates.

    **No write path may update this table directly.** It is rebuilt by the ``snapshot-rebuild``
    consumer group (``apps.portfolio.consumers``) reacting to any event whose entity is a project,
    plus ``task.*`` and ``blocker.*`` events resolved to their project. A write path that edits a
    snapshot instead of emitting the event has removed the only thing that keeps the row
    reproducible. ``rebuilt_at`` and ``last_event_id`` make a stale row traceable to the exact
    delivery that produced it.
    """

    class Health(models.TextChoices):
        """Derived project health.

        Structural, not operator data: a pure function of the open ``RiskFlag`` rows (``BLOCKED``
        if a CRITICAL flag is raised, ``AT_RISK`` if any flag is raised, otherwise ``HEALTHY``), so
        extending it would mean changing that function, not adding a taxonomy row.
        """

        HEALTHY = "HEALTHY", HEALTH_LABELS["HEALTHY"]
        AT_RISK = "AT_RISK", HEALTH_LABELS["AT_RISK"]
        BLOCKED = "BLOCKED", HEALTH_LABELS["BLOCKED"]

    project_code = models.CharField(max_length=16, primary_key=True)
    project_id = models.BigIntegerField()
    name = models.CharField(max_length=160)
    client_alias = models.CharField(max_length=96)
    engagement_type_code = models.CharField(max_length=32, db_index=True)
    engagement_type_label = models.CharField(max_length=64)
    project_type_code = models.CharField(max_length=32, blank=True, default="")
    stage_code = models.CharField(max_length=32, blank=True, default="")
    state_code = models.CharField(max_length=32)
    state_label = models.CharField(max_length=64)
    state_category = models.CharField(max_length=16, db_index=True)
    owner_code = models.CharField(max_length=32, blank=True, default="", db_index=True)
    owner_alias = models.CharField(max_length=96, blank=True, default="")
    owner_load_points = models.SmallIntegerField(default=0)
    owner_capacity_points = models.SmallIntegerField(default=0)
    start_date = models.DateField(null=True, blank=True)
    target_date = models.DateField(null=True, blank=True, db_index=True)
    business_value = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    currency = models.CharField(max_length=3, default="USD")
    next_step = models.CharField(max_length=255, blank=True, default="")
    priority_score = models.DecimalField(max_digits=5, decimal_places=2, default=0)
    priority_policy_version = models.CharField(max_length=16, blank=True, default="")
    breakdown = models.JSONField(default=dict)
    has_override = models.BooleanField(default=False)
    override_position = models.SmallIntegerField(null=True, blank=True)
    override_reason = models.CharField(max_length=255, blank=True, default="")
    risk_flags = models.JSONField(default=list)
    health = models.CharField(
        max_length=16,
        choices=Health.choices,
        default=Health.HEALTHY,
        db_index=True,
    )
    open_task_count = models.SmallIntegerField(default=0)
    overdue_task_count = models.SmallIntegerField(default=0)
    blocked_task_count = models.SmallIntegerField(default=0)
    urgent_open_task_count = models.SmallIntegerField(default=0)
    open_blocker_count = models.SmallIntegerField(default=0)
    oldest_blocker_age_days = models.SmallIntegerField(null=True, blank=True)
    last_activity_at = models.DateTimeField(null=True, blank=True)
    is_archived = models.BooleanField(default=False)
    rebuilt_at = models.DateTimeField(auto_now=True)
    last_event_id = models.UUIDField(null=True, blank=True)

    objects = ProjectSnapshotQuerySet.as_manager()

    class Meta:
        verbose_name = "project snapshot"
        verbose_name_plural = "project snapshots"
        ordering = ["-priority_score", "project_code"]
        constraints = [
            models.UniqueConstraint(
                fields=["project_id"], name="portfolio_projectsnapshot_project_id_unique"
            ),
        ]
        indexes = [
            # The whole command center in one index scan: is_archived is the constant predicate,
            # priority_score DESC matches the sort so the plan carries no sort node.
            models.Index(
                fields=["is_archived", "-priority_score"],
                name="portfolio_snap_queue",
            ),
            # ?flag=NO_TARGET_DATE filters without joining prioritization_riskflag.
            GinIndex(fields=["risk_flags"], name="portfolio_snap_riskflags_gin"),
        ]

    def __str__(self) -> str:
        """Return the business code and its queue score."""
        return f"{self.project_code} ({self.priority_score})"

    def to_queue_item(self) -> QueueItemView:
        """Describe this row as the queue entry the command center renders.

        Issues no query: the read model exists precisely so this projection reads one table. Every
        blank string is published as ``null``, because the columns default to ``""`` for storage
        reasons while the wire distinguishes "no owner" from "an owner whose code is empty"
        (`docs/API.md` §1.6).

        ``risk_flags`` is parsed defensively. It is JSONB written by the rebuild consumer, and an
        entry that no longer matches the shape is skipped rather than crashing the whole queue —
        one unreadable flag must not take the command center down.

        Returns:
            The row as an immutable :class:`~apps.portfolio.domain.views.QueueItemView`.
        """
        return QueueItemView(
            code=self.project_code,
            name=self.name,
            client_alias=self.client_alias,
            owner=(
                ActorRef.of(code=self.owner_code, label=self.owner_alias or self.owner_code)
                if self.owner_code
                else None
            ),
            engagement_type=TaxonomyRef.of(
                code=self.engagement_type_code,
                label=self.engagement_type_label or self.engagement_type_code,
            ),
            project_type_code=self.project_type_code or None,
            stage_code=self.stage_code or None,
            state=StateRef.of(
                code=self.state_code,
                label=self.state_label or self.state_code,
                category=self.state_category,
            ),
            health=HealthRef.of(self.health),
            target_date=self.target_date,
            business_value=(
                float(self.business_value) if self.business_value is not None else None
            ),
            currency=self.currency,
            next_step=self.next_step or None,
            open_tasks=self.open_task_count,
            overdue_tasks=self.overdue_task_count,
            blocked_tasks=self.blocked_task_count,
            open_blockers=self.open_blocker_count,
            score=ScoreView.from_document(
                self.breakdown,
                value=self.priority_score,
                policy_version=self.priority_policy_version,
            ),
            override=(
                QueueOverrideView(position=self.override_position, reason=self.override_reason)
                if self.has_override
                else None
            ),
            risk_flags=_risk_flag_views(self.risk_flags),
            updated_at=self.rebuilt_at,
        )


def _risk_flag_views(stored: object) -> tuple[RiskFlagView, ...]:
    """Parse ``ProjectSnapshot.risk_flags`` into projections, skipping unreadable entries.

    The column is JSONB written by the rebuild consumer from
    :class:`~apps.portfolio.domain.value_objects.RiskFlagEntry`, whose ``detail`` is the wire's
    ``reason``. An entry that does not parse is dropped rather than raised on: a single malformed
    flag must not turn the whole command center into a 500, and the missing flag is visible in the
    response next to the ones that survived.
    """
    entries = stored if isinstance(stored, list) else []
    views: list[RiskFlagView] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        code = entry.get("code")
        severity = entry.get("severity")
        if isinstance(code, str) and isinstance(severity, str):
            detail = entry.get("detail")
            views.append(
                RiskFlagView(
                    code=code,
                    severity=severity,
                    reason=detail if isinstance(detail, str) else "",
                )
            )
    return tuple(views)
