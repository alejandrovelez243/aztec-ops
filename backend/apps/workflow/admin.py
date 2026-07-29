"""Admin for the state machines — the operation's editor for its own lifecycle.

This is not decoration: ARCHITECTURE decision 6 says adding a state or an edge must not require a
deploy, and this file is where that promise is kept. States and transitions are edited inline
under their workflow so the graph is read and changed in one screen, and the state pickers on the
transition inline are narrowed to the workflow being edited so the commonest mistake — an edge
pointing into another graph — is hard to make rather than merely rejected by `clean()`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from django.contrib import admin

from apps.workflow.domain.guards import registered_guard_codes
from apps.workflow.models import Workflow, WorkflowBinding, WorkflowState, WorkflowTransition

if TYPE_CHECKING:
    from django.db.models import ForeignKey
    from django.forms import ModelChoiceField, ModelForm
    from django.http import HttpRequest


class WorkflowStateInline(admin.TabularInline[WorkflowState, Workflow]):
    """The nodes of the graph, ordered as the board shows them."""

    model = WorkflowState
    extra = 0
    fields = (
        "code",
        "label",
        "category",
        "is_initial",
        "is_terminal",
        "is_active",
        "order",
        "color",
    )
    ordering = ("order", "code")
    show_change_link = True


class WorkflowTransitionInline(admin.TabularInline[WorkflowTransition, Workflow]):
    """The edges of the graph, with their requirements and guard."""

    model = WorkflowTransition
    extra = 0
    fields = (
        "from_state",
        "to_state",
        "label",
        "requires_reason",
        "requires_fields",
        "guard",
        "is_active",
        "order",
    )
    ordering = ("from_state__order", "order")
    show_change_link = True

    def formfield_for_foreignkey(
        self, db_field: ForeignKey[Any, Any], request: HttpRequest, **kwargs: Any
    ) -> ModelChoiceField[Any] | None:
        """Offer only the states of the workflow being edited.

        Without this the pickers list every state in the database and an operator can build an
        edge across two graphs; `WorkflowTransition.clean()` rejects it, but rejecting a choice
        that should never have been offered is a worse experience than not offering it.

        The hook fires for every foreign key on the inline, not just the two state pickers, so the
        element type is left open. ``None`` is Django's own answer for a field the form excludes,
        and swallowing it here would render an excluded relation as an editable select.
        """
        if db_field.name in {"from_state", "to_state"}:
            match = request.resolver_match
            workflow_id = match.kwargs.get("object_id") if match else None
            kwargs["queryset"] = (
                WorkflowState.objects.filter(workflow_id=workflow_id).order_by("order", "code")
                if workflow_id
                else WorkflowState.objects.none()
            )
        return super().formfield_for_foreignkey(db_field, request, **kwargs)


@admin.register(Workflow)
class WorkflowAdmin(admin.ModelAdmin[Workflow]):
    """A workflow and its whole graph on one page."""

    list_display = ("code", "name", "applies_to", "is_default", "is_active", "state_count")
    list_filter = ("applies_to", "is_default", "is_active")
    search_fields = ("code", "name")
    readonly_fields = ("created_at",)
    inlines = (WorkflowStateInline, WorkflowTransitionInline)

    @admin.display(description="States")
    def state_count(self, obj: Workflow) -> int:
        """How many nodes the graph has, so an empty workflow is visible from the list."""
        return obj.states.count()


@admin.register(WorkflowState)
class WorkflowStateAdmin(admin.ModelAdmin[WorkflowState]):
    """States across all workflows, filterable by the category every other context branches on."""

    list_display = ("code", "label", "workflow", "category", "is_initial", "is_terminal", "order")
    list_filter = ("category", "workflow", "is_initial", "is_terminal")
    search_fields = ("code", "label")
    list_select_related = ("workflow",)
    ordering = ("workflow", "order", "code")


@admin.register(WorkflowTransition)
class WorkflowTransitionAdmin(admin.ModelAdmin[WorkflowTransition]):
    """Edges across all workflows: the audit view for "why can nobody move this project"."""

    list_display = (
        "workflow",
        "from_state",
        "to_state",
        "label",
        "requires_reason",
        "guard",
        "is_active",
    )
    list_filter = ("workflow", "is_active", "requires_reason", "from_state__category")
    search_fields = ("label", "guard", "from_state__code", "to_state__code")
    list_select_related = ("workflow", "from_state", "to_state")
    ordering = ("workflow", "from_state__order", "order")

    def get_form(
        self, request: HttpRequest, obj: Any = None, change: bool = False, **kwargs: Any
    ) -> type[ModelForm[WorkflowTransition]]:
        """Tell the operator which guard codes actually exist, in the field's own help text.

        The registry is the source of truth and it lives in Python, so the admin has no way to
        offer a select; naming the registered codes here is what keeps a typo from silently
        turning into `GuardNotRegistered` at the first attempted move.
        """
        form = super().get_form(request, obj, change=change, **kwargs)
        codes = ", ".join(registered_guard_codes()) or "none registered"
        form.base_fields["guard"].help_text = f"Registered guards: {codes}. Empty means no guard."
        return form


@admin.register(WorkflowBinding)
class WorkflowBindingAdmin(admin.ModelAdmin[WorkflowBinding]):
    """Which engagement type follows which graph; the null row is the per-kind default."""

    list_display = ("applies_to", "engagement_type", "workflow", "is_active")
    list_filter = ("applies_to", "is_active", "workflow")
    search_fields = ("workflow__code", "engagement_type__code")
    list_select_related = ("workflow", "engagement_type")
