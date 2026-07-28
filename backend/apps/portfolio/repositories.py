"""Every named query of the portfolio context.

Queries live here, not in ``api/`` and not inline in a service, because each one encodes a business
definition — "open task", "the queue" — and definitions that are written twice drift (PATTERNS
§2). Methods materialize their results: a repository that returns a lazy ``QuerySet`` has moved the
query back out of this file.

Two boundaries are crossed on purpose, both by :class:`OwnerLoadRepository`. It reads ``work.Task``
because owner load is a portfolio question ("who is overloaded?") whose only honest input is the
task rows, and it reads ``accounts.User.weekly_capacity_points`` — through that context's own
repository, never its manager — because that is the denominator. Capacity describes the person, so
it lives on identity; the derivation belongs to the portfolio, so it stays here. The alternative —
carrying counts on the person — is exactly the stale projection the source ``Team`` sheet already
is. The read is one aggregate query over a published column set, and nothing writes into ``work``
or ``accounts`` from here.
"""

from uuid import UUID

from django.db.models import Count, Q

from apps.accounts.repositories import users_by_codes
from apps.portfolio.domain.value_objects import (
    OwnerLoad,
    ProjectSnapshotValues,
    SnapshotQueueFilters,
)
from apps.portfolio.models import Project, ProjectSnapshot
from apps.work.models import Task
from apps.workflow.models import StateCategory

#: "Open" is the complement of this set. Read from the workflow context's own closed vocabulary
#: rather than re-listed here, so a sixth category could not silently mean "open" (DATA_MODEL §12).
_CLOSED_CATEGORIES = (StateCategory.DONE, StateCategory.CANCELLED)

#: Counts for a member the aggregate query returned no row for: they carry nothing, which is an
#: answer, not a gap.
_NO_TASKS: dict[str, int] = {
    "open_task_count": 0,
    "blocked_task_count": 0,
    "urgent_open_task_count": 0,
}

#: Joins every projection of a project needs. Kept in one place so a caller cannot half-populate a
#: result and pay for the rest one lazy query at a time.
_PROJECT_RELATIONS = (
    "client",
    "engagement_type",
    "project_type",
    "stage",
    "workflow_state",
    "owner",
)


class ProjectRepository:
    """Reads and locks of the project aggregate."""

    def get_by_code(self, project_code: str) -> Project | None:
        """Fetch a project with every relation a projection reads.

        Returns None rather than raising so the caller decides whether absence is a 404 or a
        legitimate branch; the services turn it into ``ProjectNotFound``.

        Args:
            project_code: Business code, e.g. "PRJ-01".

        Returns:
            The project, or None when no row carries that code.
        """
        return Project.objects.select_related(*_PROJECT_RELATIONS).filter(code=project_code).first()

    def get_for_update(self, project_code: str) -> Project | None:
        """Fetch and row-lock a project for the duration of the enclosing transaction.

        Used by every write use case so two concurrent updates serialize instead of last-write-wins
        on a read-modify-write. ``select_for_update`` requires an open transaction; calling this
        outside ``transaction.atomic()`` raises ``TransactionManagementError``.

        Args:
            project_code: Business code, e.g. "PRJ-01".

        Returns:
            The locked project, or None when no row carries that code.
        """
        return (
            Project.objects.select_for_update()
            .select_related(*_PROJECT_RELATIONS)
            .filter(code=project_code)
            .first()
        )

    def code_exists(self, project_code: str) -> bool:
        """Whether a project already uses this business code.

        Lets ``create_project`` raise ``DuplicateProjectCode`` instead of letting the unique
        constraint surface as a 500. The constraint is still the authority under a race.

        Args:
            project_code: Business code to test.

        Returns:
            True when the code is taken.
        """
        return Project.objects.filter(code=project_code).exists()


class OwnerLoadRepository:
    """The derived load of the people who own work: the numerator that is never stored.

    Keyed on ``accounts.User.code``, which is the code every payload, fixture and audit record
    already carries. Resolving a person by code is *not* here — that definition belongs to
    ``apps.accounts.repositories`` and is imported rather than restated.
    """

    def load_for_codes(self, member_codes: list[str]) -> dict[str, OwnerLoad]:
        """Derive current load for the given people in one aggregate query.

        "Open" is ``workflow_state.category NOT IN (DONE, CANCELLED)`` and "urgent" is
        ``priority.is_urgent`` — both structural facts read from the taxonomy, so adding a workflow
        state or renaming a priority from the admin does not touch this query (DATA_MODEL §12).

        People with no tasks at all are still present in the result with zero counts: a person
        carrying nothing is an answer to "who is overloaded?", and dropping them would make the
        caller treat "no load" as "unknown person".

        Args:
            member_codes: ``accounts.User.code`` values to evaluate. An empty list returns ``{}``
                without querying.

        Returns:
            Load per person code. Codes that match nobody are absent from the mapping.
        """
        if not member_codes:
            return {}

        capacities = {
            code: user.weekly_capacity_points for code, user in users_by_codes(member_codes).items()
        }
        if not capacities:
            return {}

        is_open = ~Q(workflow_state__category__in=_CLOSED_CATEGORIES)
        rows = (
            Task.objects.filter(assignee__code__in=capacities.keys())
            .values("assignee__code")
            .annotate(
                open_task_count=Count("pk", filter=is_open),
                blocked_task_count=Count(
                    "pk", filter=Q(workflow_state__category=StateCategory.BLOCKED)
                ),
                urgent_open_task_count=Count("pk", filter=is_open & Q(priority__is_urgent=True)),
            )
        )
        counted: dict[str, dict[str, int]] = {
            str(row["assignee__code"]): {
                "open_task_count": int(row["open_task_count"]),
                "blocked_task_count": int(row["blocked_task_count"]),
                "urgent_open_task_count": int(row["urgent_open_task_count"]),
            }
            for row in rows
        }

        return {
            code: OwnerLoad(
                member_code=code,
                open_task_count=counted.get(code, _NO_TASKS)["open_task_count"],
                blocked_task_count=counted.get(code, _NO_TASKS)["blocked_task_count"],
                urgent_open_task_count=counted.get(code, _NO_TASKS)["urgent_open_task_count"],
                weekly_capacity_points=capacity,
            )
            for code, capacity in capacities.items()
        }


class ProjectSnapshotRepository:
    """Read and write access to the denormalized command-center read model.

    The only writer that may call :meth:`write` is the ``snapshot-rebuild`` consumer group in
    ``apps.portfolio.consumers``, driven by events. No project write path updates a snapshot
    directly — doing so would make the row unreproducible from the stream and would hide a broken
    consumer behind a value that happens to look fresh (PATTERNS §8).
    """

    def write(self, values: ProjectSnapshotValues, last_event_id: UUID | None = None) -> None:
        """Upsert one snapshot row from a fully-built projection.

        Keyed on ``project_code`` rather than a numeric id so the consumer never has to resolve the
        write side first, which is also what lets the read model survive the write side being
        rebuilt. The row is replaced wholesale: a partial write would mix columns from two
        deliveries while ``last_event_id`` claimed a single one.

        Args:
            values: The complete projection for this project.
            last_event_id: Envelope id of the event that produced the projection, for tracing a
                stale row back to its delivery. None only when rebuilding outside the stream, such
                as from ``make recompute``.

        Returns:
            None.
        """
        defaults = values.model_dump(exclude={"project_code", "risk_flags"})
        defaults["risk_flags"] = [flag.model_dump() for flag in values.risk_flags]
        defaults["last_event_id"] = last_event_id
        ProjectSnapshot.objects.update_or_create(
            project_code=values.project_code, defaults=defaults
        )

    def queue(self, filters: SnapshotQueueFilters) -> list[ProjectSnapshot]:
        """Read the prioritized queue: the command center's entire main view, in one index scan.

        Archived projects are excluded unconditionally — they are out of the operation's attention
        by definition, and making that a parameter would invite a caller to rank them. The ordering
        matches the ``(is_archived, priority_score DESC)`` index, so the plan carries no sort node.

        Args:
            filters: Facets and window. Any facet left None does not filter.

        Returns:
            Snapshots ordered by score descending, ties broken by code so paging is stable.
        """
        queryset = ProjectSnapshot.objects.filter(is_archived=False)

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

        window = filters.offset + filters.limit
        return list(queryset.order_by("-priority_score", "project_code")[filters.offset : window])

    def get(self, project_code: str) -> ProjectSnapshot | None:
        """Read one snapshot by business code.

        Args:
            project_code: Business code, e.g. "PRJ-01".

        Returns:
            The snapshot, or None when the rebuild consumer has not produced one yet.
        """
        return ProjectSnapshot.objects.filter(project_code=project_code).first()
