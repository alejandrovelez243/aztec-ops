"""Admin for the ``work`` context.

This is the operation's editing surface for the two things it genuinely fixes by hand:
dependencies the fixture generator could not resolve to a task, and blockers that need an
owner or a correction. Everything is filterable by the facets the morning review actually
uses — priority, state, assignee, and whether a blocker is still open.

``workflow_state`` is intentionally **not** editable here. It is written only by the
transition service, and an admin form that assigned it would be the one path around the
declared state machine. It is shown, and it is read-only.

``Task.assignee`` and ``Blocker.owner`` are ``accounts.User`` foreign keys since the roster
was merged into identity, so the ``raw_id_fields`` lookup popups and the ``list_filter``
dropdowns resolve against ``accounts.admin.PersonAdmin``. They stay ``raw_id_fields`` rather
than becoming ``autocomplete_fields``: the widget is unchanged by the merge and the target is
registered either way.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from django.contrib import admin

from apps.work.models import Blocker, Note, Task, TaskDependency

if TYPE_CHECKING:
    from django.db.models import QuerySet
    from django.http import HttpRequest


class ResolutionStateFilter(admin.SimpleListFilter):
    """Filter blockers by whether they are still open.

    Openness is ``resolved_at IS NULL`` and never a substring of the description, so this
    filter is the admin's view of exactly the predicate the blockage signal and the risk
    evaluator use.
    """

    title = "resolution state"
    parameter_name = "resolution_state"

    def lookups(
        self,
        request: HttpRequest,  # noqa: ARG002
        model_admin: admin.ModelAdmin[Any],  # noqa: ARG002
    ) -> list[tuple[str, str]]:
        """Return the two states a blocker can be in."""
        return [("open", "Open"), ("resolved", "Resolved")]

    def queryset(self, request: HttpRequest, queryset: QuerySet[Blocker]) -> QuerySet[Blocker]:  # noqa: ARG002
        """Narrow to open or resolved blockers; an absent parameter narrows nothing."""
        if self.value() == "open":
            return queryset.filter(resolved_at__isnull=True)
        if self.value() == "resolved":
            return queryset.filter(resolved_at__isnull=False)
        return queryset


class TaskDependencyInline(admin.TabularInline[TaskDependency, Task]):
    """Prerequisites of a task, edited where the task itself is edited."""

    model = TaskDependency
    fk_name = "task"
    extra = 0
    raw_id_fields = ("depends_on",)
    fields = ("depends_on", "raw_label", "is_resolved", "created_at")
    readonly_fields = ("created_at",)


class BlockerInline(admin.TabularInline[Blocker, Task]):
    """Blockers raised on this task, read-only: they are opened and closed by the services."""

    model = Blocker
    fk_name = "task"
    extra = 0
    fields = ("kind", "description", "owner", "raised_at", "resolved_at", "resolution_reason")
    readonly_fields = fields
    can_delete = False

    def has_add_permission(  # type: ignore[override]  # django-stubs types the ModelAdmin arity here, not the inline's (request, obj)
        self,
        request: HttpRequest,  # noqa: ARG002
        obj: Task | None,  # noqa: ARG002
    ) -> bool:
        """Refuse inline creation so a blocker always goes through ``raise_blocker``."""
        return False


@admin.register(Task)
class TaskAdmin(admin.ModelAdmin[Task]):
    """Task list and detail."""

    list_display = (
        "code",
        "title",
        "project",
        "priority",
        "workflow_state",
        "assignee",
        "due_date",
        "is_archived",
        "updated_at",
    )
    list_filter = ("priority", "workflow_state", "assignee", "is_archived")
    list_select_related = ("project", "priority", "workflow_state", "assignee")
    search_fields = ("code", "title")
    ordering = ("code",)
    date_hierarchy = "due_date"
    raw_id_fields = ("project", "assignee")
    # ``workflow`` is shown and never edited, exactly as ``workflow_state`` is once the task
    # exists: assigning a lifecycle writes an ``ActivityRecord`` and publishes an event, and an
    # editable select here would skip both. The write is ``PUT /api/v1/tasks/{code}/workflow``.
    readonly_fields = ("workflow", "created_at", "updated_at")
    inlines = (TaskDependencyInline, BlockerInline)
    fieldsets = (
        (None, {"fields": ("code", "project", "title", "detail")}),
        ("Assignment", {"fields": ("assignee", "priority", "due_date")}),
        ("State", {"fields": ("workflow", "workflow_state", "last_progress", "is_archived")}),
        ("Timestamps", {"fields": ("created_at", "updated_at")}),
    )

    def get_readonly_fields(self, request: HttpRequest, obj: Task | None = None) -> tuple[str, ...]:  # noqa: ARG002
        """Freeze ``workflow_state`` once the task exists.

        A new task has to be given a starting state for the row to be insertable at all,
        but after that the column belongs to the transition service: an editable select
        here would be the one path around the declared state machine, and it would leave no
        ``ActivityRecord`` and no event behind it.
        """
        if obj is None:
            return self.readonly_fields
        return (*self.readonly_fields, "workflow_state")


@admin.register(TaskDependency)
class TaskDependencyAdmin(admin.ModelAdmin[TaskDependency]):
    """Dependency edges, including the ones that never resolved to a task.

    ``is_resolved`` leads the filters because the useful question here is "which of the 61
    free-text dependencies still point at nothing", and that is one filter rather than a
    null scan.
    """

    list_display = ("task", "depends_on", "raw_label", "is_resolved", "created_at")
    list_filter = ("is_resolved",)
    list_select_related = ("task", "depends_on")
    search_fields = ("task__code", "task__title", "depends_on__code", "raw_label")
    raw_id_fields = ("task", "depends_on")
    readonly_fields = ("created_at",)


@admin.register(Blocker)
class BlockerAdmin(admin.ModelAdmin[Blocker]):
    """Blockers, open and resolved.

    ``raised_at`` is read-only: it is when the impediment started, and editing it would
    rewrite the age that drives the blockage signal and the "blocked for N days" badge.
    """

    list_display = ("__str__", "project", "task", "kind", "owner", "raised_at", "resolved_at")
    list_filter = (ResolutionStateFilter, "kind", "owner")
    list_select_related = ("project", "task", "owner")
    search_fields = ("description", "project__code", "task__code", "resolution_reason")
    ordering = ("-raised_at",)
    date_hierarchy = "raised_at"
    raw_id_fields = ("project", "task", "owner")
    readonly_fields = ("raised_at",)


@admin.register(Note)
class NoteAdmin(admin.ModelAdmin[Note]):
    """Notes, newest first.

    Append-only in practice: ``author`` and ``created_at`` are read-only, because a note is
    a record of who said what and when, and a mutable one is not worth reading.
    """

    list_display = ("__str__", "project", "task", "author", "created_at")
    list_filter = ("author",)
    list_select_related = ("project", "task")
    search_fields = ("body", "project__code", "task__code", "author")
    ordering = ("-created_at",)
    date_hierarchy = "created_at"
    raw_id_fields = ("project", "task")
    readonly_fields = ("author", "created_at")
