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

from apps.portfolio.domain.value_objects import ProjectResult

if TYPE_CHECKING:
    from apps.portfolio.domain.value_objects import ProjectSnapshotValues, SnapshotQueueFilters

#: Joins every projection of a project reads. Kept in one place so a caller cannot half-populate a
#: result and then pay for the rest one lazy query at a time.
_PROJECT_RELATIONS = (
    "client",
    "engagement_type",
    "project_type",
    "stage",
    "workflow_state",
    "owner",
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
        """Apply the command center's facets. A facet left ``None`` does not filter.

        Args:
            filters: The requested facets. The window (``offset``/``limit``) is not applied
                here — chain :meth:`page` — so a caller can count before paging.
        """
        queryset = self
        if filters.health is not None:
            queryset = queryset.filter(health=filters.health)
        if filters.state_category is not None:
            queryset = queryset.filter(state_category=filters.state_category)
        if filters.engagement_type_code is not None:
            queryset = queryset.filter(engagement_type_code=filters.engagement_type_code)
        if filters.owner_code is not None:
            queryset = queryset.filter(owner_code=filters.owner_code)
        if filters.risk_flag_code is not None:
            # GIN containment, so ?flag=BLOCKED never joins prioritization_riskflag.
            queryset = queryset.filter(risk_flags__contains=[{"code": filters.risk_flag_code}])
        return queryset

    def in_queue_order(self) -> "ProjectSnapshotQuerySet":
        """Order as the queue: score descending, ties broken by code so paging is stable.

        Matches the ``(is_archived, priority_score DESC)`` index, so combined with
        :meth:`in_attention` the plan carries no sort node.
        """
        return self.order_by("-priority_score", "project_code")

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
                stream, such as from ``make recompute``.
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
    currency = models.CharField(max_length=3, default="USD")
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

        The six relations read below must already be loaded: ``ProjectQuerySet.with_relations``
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
            currency=self.currency,
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

        HEALTHY = "HEALTHY", "Healthy"
        AT_RISK = "AT_RISK", "At risk"
        BLOCKED = "BLOCKED", "Blocked"

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
