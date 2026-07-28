"""Admin configuration for the audit trail.

The admin is where the operation investigates "what happened to PRJ-01 last week" and "what
else moved when this was deprioritized", so the filters and the search fields are the feature.
Writing, on the other hand, is closed on every path: this is the one model in the project the
admin may only read.
"""

from typing import TYPE_CHECKING

from django.contrib import admin
from django.http import HttpRequest

from apps.activity.models import ActivityRecord

if TYPE_CHECKING:
    # django-stubs types ModelAdmin as generic, but the runtime class is not subscriptable
    # unless django_stubs_ext.monkeypatch() has been called. Parameterizing only under
    # TYPE_CHECKING keeps mypy precise without making the admin depend on that call.
    ModelAdminBase = admin.ModelAdmin[ActivityRecord]
else:
    ModelAdminBase = admin.ModelAdmin


@admin.register(ActivityRecord)
class ActivityRecordAdmin(ModelAdminBase):
    """Read-only view over ``activity_activityrecord``.

    Every field is readonly and all three write permissions return ``False``. An audit trail
    that can be edited is not an audit trail — a superuser quietly fixing a ``from_value`` in
    the admin is exactly the failure the table's ``save()``/``delete()`` overrides exist to
    prevent, and leaving the admin writable would route around them.
    """

    list_display = (
        "occurred_at",
        "entity_type",
        "entity_id",
        "verb",
        "origin",
        "actor",
        "from_value",
        "to_value",
        "correlation_id",
    )
    list_filter = ("verb", "origin", "entity_type", "occurred_at", "actor")
    search_fields = ("entity_id", "actor", "reason", "from_value", "to_value", "correlation_id")
    date_hierarchy = "occurred_at"
    ordering = ("-occurred_at", "-id")
    list_per_page = 50
    # Everything, including the primary key: the append-only rule has no exempt column.
    readonly_fields = (
        "id",
        "entity_type",
        "entity_id",
        "verb",
        "origin",
        "actor",
        "from_value",
        "to_value",
        "reason",
        "metadata",
        "occurred_at",
        "correlation_id",
    )

    def has_add_permission(self, _request: HttpRequest) -> bool:
        """Deny adding: records are appended by services, which also write the matching event.

        A hand-typed record would have no ``OutboxEvent`` beside it, so the trail and the
        stream would describe different histories.
        """
        return False

    def has_change_permission(
        self, _request: HttpRequest, _obj: ActivityRecord | None = None
    ) -> bool:
        """Deny editing: a record is a point-in-time fact, and corrections are appended."""
        return False

    def has_delete_permission(
        self, _request: HttpRequest, _obj: ActivityRecord | None = None
    ) -> bool:
        """Deny deletion: a trail whose rows can vanish cannot prove anything about the gaps."""
        return False
