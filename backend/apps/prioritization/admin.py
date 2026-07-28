"""Admin for the prioritization context.

The policy is the one editable thing here: tuning the ranking is an operational decision, so it
happens by inserting a new ``PriorityPolicy`` version from this screen rather than by a deploy.
Scores and risk flags are engine output — they are shown in full, with the breakdown rendered
readably, and are never editable, because a hand-edited score is a score that no longer matches
the reasons printed beside it.
"""

import json
from typing import ClassVar

from django.contrib import admin
from django.db.models import QuerySet
from django.http import HttpRequest
from django.utils.html import format_html
from django.utils.safestring import SafeString

from .models import PriorityOverride, PriorityPolicy, PriorityScore, RiskFlag


def _render_json(document: object) -> SafeString:
    """Render a JSONB document as indented, monospaced text.

    The breakdown is the artifact that justifies a rank; a one-line dump of it is technically the
    same data and practically unreadable, which is how "the score is explainable" stops being
    true in the only screen an operator actually opens.
    """
    return format_html(
        '<pre style="white-space:pre-wrap;margin:0">{}</pre>',
        json.dumps(document, indent=2, ensure_ascii=False, sort_keys=False),
    )


@admin.register(PriorityPolicy)
class PriorityPolicyAdmin(admin.ModelAdmin[PriorityPolicy]):
    """Weights are tuned here, as a new version, without a deploy."""

    list_display = ("version", "is_active", "weight_summary", "created_at")
    list_filter = ("is_active",)
    search_fields = ("version", "notes")
    readonly_fields: ClassVar[tuple[str, ...]] = ("created_at",)
    # Unannotated on purpose: the base declares ``fieldsets`` as a sequence of
    # ``(header, options)`` pairs whose options are a TypedDict, and a hand-written annotation can
    # only restate it less precisely.
    fieldsets = (
        (None, {"fields": ("version", "is_active")}),
        ("Criterion", {"fields": ("weights", "modifiers", "notes")}),
        ("Audit", {"fields": ("created_at",)}),
    )

    @admin.display(description="weights")
    def weight_summary(self, obj: PriorityPolicy) -> str:
        """The weights on one line, heaviest first, so two versions can be compared at a glance."""
        weights: dict[str, float] = obj.weights or {}
        ordered = sorted(weights.items(), key=lambda item: -item[1])
        return ", ".join(f"{code} {weight}" for code, weight in ordered)


@admin.register(PriorityScore)
class PriorityScoreAdmin(admin.ModelAdmin[PriorityScore]):
    """Engine output. Read-only everywhere, including the breakdown."""

    list_display = (
        "project",
        "value",
        "policy_version",
        "modifier_total",
        "computed_at",
        "valid_until",
    )
    list_filter = ("policy_version",)
    search_fields = ("project__code", "project__name")
    date_hierarchy = "computed_at"
    readonly_fields: ClassVar[tuple[str, ...]] = (
        "project",
        "value",
        "policy_version",
        "modifier_total",
        "computed_at",
        "input_hash",
        "valid_until",
        "breakdown_document",
    )
    exclude: ClassVar[tuple[str, ...]] = ("breakdown",)

    def has_add_permission(self, request: HttpRequest) -> bool:  # noqa: ARG002  Django's hook signature
        """Scores are computed, never entered by hand."""
        return False

    def has_change_permission(self, request: HttpRequest, obj: PriorityScore | None = None) -> bool:  # noqa: ARG002  Django's hook signature
        """A hand-edited score would contradict the reasons stored beside it."""
        return False

    def get_queryset(self, request: HttpRequest) -> QuerySet[PriorityScore]:
        """Join the project once instead of per row in ``list_display``."""
        return super().get_queryset(request).select_related("project")

    @admin.display(description="breakdown")
    def breakdown_document(self, obj: PriorityScore) -> SafeString:
        """The per-signal reasons, rendered so a rank can be defended from this page alone."""
        return _render_json(obj.breakdown)


@admin.register(PriorityOverride)
class PriorityOverrideAdmin(admin.ModelAdmin[PriorityOverride]):
    """Who forced what, why, and whether it is still in force."""

    list_display = ("project", "position", "boost", "actor", "reason", "expires_at", "revoked_at")
    list_filter = ("actor", "revoked_at")
    search_fields = ("project__code", "project__name", "actor", "reason")
    date_hierarchy = "created_at"
    readonly_fields: ClassVar[tuple[str, ...]] = ("created_at",)

    def get_queryset(self, request: HttpRequest) -> QuerySet[PriorityOverride]:
        """Join the project once instead of per row in ``list_display``."""
        return super().get_queryset(request).select_related("project")


@admin.register(RiskFlag)
class RiskFlagAdmin(admin.ModelAdmin[RiskFlag]):
    """Detections, raised and cleared as separate rows. Read-only: severity comes from the registry."""

    list_display = ("project", "code", "severity", "detail", "detected_at", "cleared_at")
    list_filter = ("code", "severity", "cleared_at")
    search_fields = ("project__code", "project__name", "detail")
    date_hierarchy = "detected_at"
    readonly_fields: ClassVar[tuple[str, ...]] = (
        "project",
        "code",
        "severity",
        "detail",
        "detected_at",
        "cleared_at",
    )

    def has_add_permission(self, request: HttpRequest) -> bool:  # noqa: ARG002  Django's hook signature
        """Flags are raised by the risk evaluator, not typed in."""
        return False

    def has_change_permission(self, request: HttpRequest, obj: RiskFlag | None = None) -> bool:  # noqa: ARG002  Django's hook signature
        """Clearing a flag by hand would leave the specification raising it again on the next tick."""
        return False

    def get_queryset(self, request: HttpRequest) -> QuerySet[RiskFlag]:
        """Join the project once instead of per row in ``list_display``."""
        return super().get_queryset(request).select_related("project")
