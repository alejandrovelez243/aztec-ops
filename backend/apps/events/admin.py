"""Admin views for the bus.

Both tables are append-only from the admin's point of view: an outbox row records what a
transaction committed and a processed-event row is an idempotency claim, so editing either would
rewrite history or cause a replay. Everything here is therefore read-only, and the value it adds is
diagnostic — a stuck event has to be visible without opening a database shell (`docs/RUNBOOK.md`).

There is one write, and it is the reason this module matters more than it used to. Dropping Redis
Streams cost us stream replay; the replacement is the *re-queue* action below, which puts a
dead-lettered row back on the drain's claim index. It is better than replaying a stream: the row
lives in PostgreSQL, it is queryable, it carries ``attempts`` and ``last_error``, and the handlers
that already succeeded hold a ``ProcessedEvent`` claim so the replay only re-runs the one that
failed.
"""

from typing import Any, ClassVar

from django.contrib import admin, messages
from django.db.models import QuerySet
from django.http import HttpRequest

from apps.events.models import OutboxEvent, OutboxEventQuerySet, ProcessedEvent

_ERROR_PREVIEW_LIMIT = 60


class PublicationStateFilter(admin.SimpleListFilter):
    """Filter outbox rows by where they are in the delivery lifecycle.

    ``published_at`` alone cannot answer the question an operator actually asks, because a
    dead-lettered row is also marked published — it was dispatched and then a handler could not
    apply it. The three buckets here map to the three real outcomes.
    """

    title = "delivery state"
    parameter_name = "delivery_state"

    def lookups(
        self, _request: HttpRequest, _model_admin: admin.ModelAdmin[Any]
    ) -> list[tuple[str, str]]:
        """The three outcomes a row can be in."""
        return [
            ("pending", "Pending (not yet dispatched)"),
            ("dispatched", "Dispatched"),
            ("dead_lettered", "Dead lettered"),
        ]

    def queryset(
        self, _request: HttpRequest, queryset: QuerySet[OutboxEvent]
    ) -> QuerySet[OutboxEvent]:
        """Narrow to the chosen bucket; an unset filter shows everything."""
        if self.value() == "pending":
            return queryset.filter(published_at__isnull=True)
        if self.value() == "dispatched":
            return queryset.filter(published_at__isnull=False, dead_lettered_at__isnull=True)
        if self.value() == "dead_lettered":
            return queryset.filter(dead_lettered_at__isnull=False)
        return queryset


@admin.register(OutboxEvent)
class OutboxEventAdmin(admin.ModelAdmin[OutboxEvent]):
    """Read-only view of the outbox, tuned for the three questions the bus raises.

    "Is the drain behind?" is the *Pending* filter with a rising ``attempts``. "What is stuck?" is
    the *Dead lettered* filter, and the answer to it is the re-queue action. "What did we publish
    for PRJ-01?" is the topic filter plus a search on the entity id — all backed by the indexes in
    DATA_MODEL §10.3, so none of them degrades as history accumulates.
    """

    list_display = (
        "topic",
        "entity_type",
        "entity_id",
        "actor",
        "version",
        "occurred_at",
        "published_at",
        "dead_lettered_at",
        "attempts",
        "error_preview",
    )
    list_filter = (
        PublicationStateFilter,
        "topic",
        "entity_type",
        "version",
        "occurred_at",
    )
    search_fields = ("id", "entity_id", "correlation_id", "actor", "last_error")
    date_hierarchy = "occurred_at"
    ordering = ("-occurred_at",)
    list_per_page = 50
    actions = ("requeue_dead_lettered",)
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
        "dead_lettered_at",
        "attempts",
        "last_error",
    )

    @admin.display(description="last error")
    def error_preview(self, obj: OutboxEvent) -> str:
        """Show enough of the failure to recognise it in a list, not enough to break the layout."""
        if len(obj.last_error) <= _ERROR_PREVIEW_LIMIT:
            return obj.last_error
        return f"{obj.last_error[:_ERROR_PREVIEW_LIMIT]}…"

    @admin.action(description="Re-queue selected dead-lettered events")
    def requeue_dead_lettered(self, request: HttpRequest, queryset: OutboxEventQuerySet) -> None:
        """Put dead-lettered rows back on the claim index so the drain redelivers them.

        Only dead-lettered rows are touched: re-queuing a healthy row would redeliver an event
        whose handlers are still running, and while the ledger would absorb it, the action would
        be lying about what it did. A selection with none of them says so rather than reporting a
        successful no-op.

        Args:
            request: The admin request, for the result message.
            queryset: The selected rows, filtered here rather than in the template.
        """
        requeued = 0
        for row in queryset.dead_lettered():
            row.requeue()
            requeued += 1
        if requeued == 0:
            self.message_user(
                request,
                "Nothing to re-queue: none of the selected events is dead lettered.",
                level=messages.WARNING,
            )
            return
        self.message_user(
            request,
            f"Re-queued {requeued} event(s). Handlers that already applied them will skip.",
            level=messages.SUCCESS,
        )

    def has_add_permission(self, _request: HttpRequest) -> bool:
        """Events are written by services inside their own transaction, never by hand."""
        return False

    def has_change_permission(self, _request: HttpRequest, _obj: OutboxEvent | None = None) -> bool:
        """An event is a fact; the only supported write is the re-queue action."""
        return False

    def has_delete_permission(self, _request: HttpRequest, _obj: OutboxEvent | None = None) -> bool:
        """Deleting an unpublished row loses the event silently, which is what the outbox prevents."""
        return False


@admin.register(ProcessedEvent)
class ProcessedEventAdmin(admin.ModelAdmin[ProcessedEvent]):
    """Read-only view of the idempotency ledger.

    Answers "did this handler already apply this event?" without a shell, which is the first
    question when a reactor looks like it skipped something. Deleting a row here would make the
    handler reapply the event on the next redelivery, so the ledger is not editable.
    """

    list_display = ("event_id", "handler", "processed_at")
    list_filter = ("handler", "processed_at")
    search_fields = ("event_id", "handler")
    date_hierarchy = "processed_at"
    ordering = ("-processed_at",)
    list_per_page = 50
    readonly_fields: ClassVar[tuple[str, ...]] = ("event_id", "handler", "processed_at")

    def has_add_permission(self, _request: HttpRequest) -> bool:
        """Claims are made by ``events.handle_event`` inside the handler's transaction."""
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
