"""Admin configuration for the taxonomies.

This admin is the whole point of keeping the taxonomies in the database: it is where the operation
renames a label, recolors a chip, reorders a menu or retires a value without a deploy. It is
therefore tuned for one screen of bulk editing — ``order`` and ``is_active`` are editable straight
from the changelist, so reordering five stages is one form submit rather than five page loads.

``code`` is never editable in bulk: it is the value every branch in the code base compares against,
so changing it is a deliberate act on the detail page, not something done while dragging numbers
around.
"""

from typing import ClassVar

from django.contrib import admin
from django.db.models import Model
from django.utils.html import format_html

from apps.catalog.models import (
    EngagementType,
    Priority,
    ProjectType,
    Role,
    Stage,
    TaxonomyBase,
)


class TaxonomyAdmin(admin.ModelAdmin[TaxonomyBase]):
    """Shared changelist for every taxonomy: reorder, retire and recolor in place.

    The changelist tuples are annotated as variable-length on purpose. Left to inference each one
    would be fixed at the arity written here, and every subclass that shows one extra column —
    ``weight``, ``is_urgent`` — would be redeclaring a narrower attribute. The base states what a
    taxonomy changelist is: some columns, some filters.
    """

    list_display: tuple[str, ...] = ("code", "label", "order", "is_active", "color", "swatch")
    list_display_links: tuple[str, ...] = ("code",)
    list_editable: ClassVar[tuple[str, ...]] = ("order", "is_active")
    list_filter: ClassVar[tuple[str, ...]] = ("is_active",)
    search_fields = ("code", "label")
    ordering = ("order", "code")
    save_on_top = True

    @admin.display(description="swatch")
    def swatch(self, obj: Model) -> str:
        """Render ``color`` as the chip the UI will actually draw.

        A hex string tells an operator nothing about whether two taxonomies are distinguishable at
        a glance; the rendered square does. Returns a dash when no color is set, rather than an
        invisible box that reads as a rendering bug.
        """
        color = getattr(obj, "color", "")
        if not color:
            return "—"
        return format_html(
            '<span style="display:inline-block;width:1rem;height:1rem;'
            'border:1px solid #999;background:{}"></span>',
            color,
        )


@admin.register(EngagementType)
class EngagementTypeAdmin(TaxonomyAdmin):
    """Engagement types, with the score multiplier visible next to the row it multiplies."""

    list_display = ("code", "label", "weight", "order", "is_active", "color", "swatch")


@admin.register(ProjectType)
class ProjectTypeAdmin(TaxonomyAdmin):
    """Project types — classification only, so the base changelist is the whole story."""


@admin.register(Stage)
class StageAdmin(TaxonomyAdmin):
    """Stages, where ``order`` decides which workflow state a seeded project starts in."""


@admin.register(Priority)
class PriorityAdmin(TaxonomyAdmin):
    """Priorities, with ``is_urgent`` editable because it is what ``criticality`` counts."""

    list_display = (
        "code",
        "label",
        "weight",
        "is_urgent",
        "order",
        "is_active",
        "color",
        "swatch",
    )
    list_editable = ("order", "is_active", "is_urgent")
    list_filter = ("is_active", "is_urgent")


@admin.register(Role)
class RoleAdmin(TaxonomyAdmin):
    """Roles assigned to team members."""
