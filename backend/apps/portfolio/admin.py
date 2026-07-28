"""Admin configuration for the portfolio context.

The admin is how the operation corrects project data without a deploy, so it is configured to be
usable rather than merely registered. The roster is not edited here: a project owner is an
``accounts.User``, registered by ``apps.accounts.admin``. ``ProjectAdmin.autocomplete_fields``
depends on that registration supplying ``search_fields``, which it does.

Two deliberate restrictions:

* ``Project.workflow_state`` is read-only. The admin is the most tempting place to bypass the
  transition service, and a state assigned here would produce a project in a state no
  ``WorkflowTransition`` allows, with no ``ActivityRecord`` and no event (CLAUDE.md rule 2).
  State moves happen through the API's transition endpoint.
* ``ProjectSnapshot`` is fully read-only and cannot be added or deleted. It is a projection rebuilt
  by the ``snapshot-rebuild`` consumer; an edit here would be silently overwritten by the next
  event, which is worse than being impossible.
"""

from django.contrib import admin
from django.http import HttpRequest

from apps.portfolio.models import Client, Project, ProjectSnapshot


@admin.register(Client)
class ClientAdmin(admin.ModelAdmin[Client]):
    """Counterparties. ``code`` is the fixture key, so it leads the list before the display alias."""

    list_display = ("code", "alias", "is_active", "created_at")
    list_filter = ("is_active",)
    search_fields = ("code", "alias", "notes")
    ordering = ("alias",)
    readonly_fields = ("created_at",)


@admin.register(Project)
class ProjectAdmin(admin.ModelAdmin[Project]):
    """The aggregate root, filterable along the facets the operation actually sorts by."""

    list_display = ("code", "client", "owner", "workflow_state", "target_date", "is_archived")
    list_filter = ("engagement_type", "stage", "workflow_state", "is_archived", "project_type")
    search_fields = ("code", "name")
    list_select_related = ("client", "owner", "workflow_state", "engagement_type", "stage")
    autocomplete_fields = ("client", "owner")
    date_hierarchy = "target_date"
    ordering = ("code",)
    readonly_fields = ("workflow_state", "created_at", "updated_at")
    fieldsets = (
        (None, {"fields": ("code", "name", "client", "summary")}),
        (
            "Classification",
            {"fields": ("engagement_type", "project_type", "stage", "workflow_state")},
        ),
        ("Delivery", {"fields": ("owner", "start_date", "target_date", "next_step")}),
        ("Commercial", {"fields": ("business_value", "currency")}),
        (
            "Lifecycle",
            {"fields": ("is_archived", "imported_health", "created_at", "updated_at")},
        ),
    )


@admin.register(ProjectSnapshot)
class ProjectSnapshotAdmin(admin.ModelAdmin[ProjectSnapshot]):
    """The read model, exposed for diagnosis only.

    A ``rebuilt_at`` lagging a busy stream is the visible symptom of a stuck ``snapshot-rebuild``
    consumer, and ``last_event_id`` names the delivery that produced the row — which is the whole
    reason this table is in the admin at all.
    """

    list_display = (
        "project_code",
        "name",
        "health",
        "priority_score",
        "state_code",
        "owner_code",
        "rebuilt_at",
    )
    list_filter = ("health", "state_category", "engagement_type_code", "is_archived")
    search_fields = ("project_code", "name", "client_alias", "owner_code")
    ordering = ("-priority_score", "project_code")

    # The unused arguments below are Django's fixed ModelAdmin signatures, not stale parameters.
    def get_readonly_fields(
        self,
        request: HttpRequest,  # noqa: ARG002
        obj: ProjectSnapshot | None = None,  # noqa: ARG002
    ) -> tuple[str, ...]:
        """Return every field: the whole row is rebuilt from events, so none of it is editable."""
        return tuple(field.name for field in self.model._meta.fields)

    def has_add_permission(self, request: HttpRequest) -> bool:  # noqa: ARG002
        """Refuse manual inserts: a snapshot with no project behind it can never be rebuilt."""
        return False

    def has_change_permission(
        self,
        request: HttpRequest,  # noqa: ARG002
        obj: ProjectSnapshot | None = None,  # noqa: ARG002
    ) -> bool:
        """Refuse edits: the next event would overwrite them without a trace."""
        return False

    def has_delete_permission(
        self,
        request: HttpRequest,  # noqa: ARG002
        obj: ProjectSnapshot | None = None,  # noqa: ARG002
    ) -> bool:
        """Refuse deletes: removing a row hides a project from the command center silently."""
        return False
