"""Admin views for the bus.

Both tables are append-only from the admin's point of view: an outbox row records what a
transaction committed and a processed-event row is an idempotency claim, so editing either would
rewrite history or cause a replay. Everything here is therefore read-only, and the value it adds
is diagnostic — a stuck event has to be visible without opening a database shell
(`docs/RUNBOOK.md`).
"""

from typing import Any, ClassVar

from django.contrib import admin
from django.db.models import QuerySet
from django.http import HttpRequest

from apps.events.models import OutboxEvent, ProcessedEvent

_ERROR_PREVIEW_LIMIT = 60


class PublicationStateFilter(admin.SimpleListFilter):
    """Filter outbox rows by where they are in the publication lifecycle.

    ``published_at`` alone cannot answer the question an operator actually asks, because a dead
    lettered row is also marked published — it left the claim index without reaching
    ``aztec.events``. The three buckets here map to the three real outcomes.
    """

    title = "publication state"
    parameter_name = "publication_state"

    def lookups(
        self, _request: HttpRequest, _model_admin: admin.ModelAdmin[Any]
    ) -> list[tuple[str, str]]:
        """The three outcomes a row can be in."""
        return [
            ("pending", "Pending (not yet on the stream)"),
            ("published", "Published"),
            ("dead_lettered", "Dead lettered"),
        ]

    def queryset(
        self, _request: HttpRequest, queryset: QuerySet[OutboxEvent]
    ) -> QuerySet[OutboxEvent]:
        """Narrow to the chosen bucket; an unset filter shows everything."""
        if self.value() == "pending":
            return queryset.filter(published_at__isnull=True)
        if self.value() == "published":
            return queryset.filter(published_at__isnull=False, last_error="")
        if self.value() == "dead_lettered":
            return queryset.filter(published_at__isnull=False).exclude(last_error="")
        return queryset


@admin.register(OutboxEvent)
class OutboxEventAdmin(admin.ModelAdmin[OutboxEvent]):
    """Read-only view of the outbox, tuned for the two questions the relay raises.

    "Is the relay behind?" is the *Pending* filter with a rising ``attempts``. "What did we
    publish for PRJ-01?" is the topic filter plus a search on the entity id — both backed by the
    indexes in DATA_MODEL §10.3, so neither degrades as history accumulates.
    """

    list_display = (
        "topic",
        "entity_type",
        "entity_id",
        "actor",
        "version",
        "occurred_at",
        "published_at",
        "attempts",
        "error_preview",
    )
    list_filter = (PublicationStateFilter, "topic", "entity_type", "version", "occurred_at")
    search_fields = ("id", "entity_id", "correlation_id", "actor", "last_error")
    date_hierarchy = "occurred_at"
    ordering = ("-occurred_at",)
    list_per_page = 50
    readonly_fields: ClassVar[tuple[str, ...]] = (
        "id",
        "topic",
        "entity_type",
        "entity_id",
        "payload",
        "actor",
        "correlation_id",
        "version",
        "occurred_at",
        "created_at",
        "published_at",
        "stream_entry_id",
        "attempts",
        "last_error",
    )

    @admin.display(description="last error")
    def error_preview(self, obj: OutboxEvent) -> str:
        """Show enough of the failure to recognise it in a list, not enough to break the layout."""
        if len(obj.last_error) <= _ERROR_PREVIEW_LIMIT:
            return obj.last_error
        return f"{obj.last_error[:_ERROR_PREVIEW_LIMIT]}…"

    def has_add_permission(self, _request: HttpRequest) -> bool:
        """Events are written by services inside their own transaction, never by hand."""
        return False

    def has_change_permission(self, _request: HttpRequest, _obj: OutboxEvent | None = None) -> bool:
        """A published event is a fact; editing the row would not unpublish anything."""
        return False

    def has_delete_permission(self, _request: HttpRequest, _obj: OutboxEvent | None = None) -> bool:
        """Deleting an unpublished row loses the event silently, which is what the outbox prevents."""
        return False


@admin.register(ProcessedEvent)
class ProcessedEventAdmin(admin.ModelAdmin[ProcessedEvent]):
    """Read-only view of the idempotency ledger.

    Answers "did this group already apply this event?" without a shell, which is the first
    question when a consumer looks like it skipped something. Deleting a row here would make the
    group reapply the event on the next redelivery, so the ledger is not editable.
    """

    list_display = ("event_id", "consumer_group", "processed_at")
    list_filter = ("consumer_group", "processed_at")
    search_fields = ("event_id", "consumer_group")
    date_hierarchy = "processed_at"
    ordering = ("-processed_at",)
    list_per_page = 50
    readonly_fields: ClassVar[tuple[str, ...]] = ("event_id", "consumer_group", "processed_at")

    def has_add_permission(self, _request: HttpRequest) -> bool:
        """Claims are made by the consumer runner inside the handler's transaction."""
        return False

    def has_change_permission(
        self, _request: HttpRequest, _obj: ProcessedEvent | None = None
    ) -> bool:
        """Re-pointing a claim at another event makes two events look processed and one not."""
        return False

    def has_delete_permission(
        self, _request: HttpRequest, _obj: ProcessedEvent | None = None
    ) -> bool:
        """Retention is a scheduled sweep on ``processed_at``, not a manual delete."""
        return False
