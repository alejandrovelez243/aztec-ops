"""Every query the workflow context needs, in one place.

Two of them encode a business definition and therefore may not be written anywhere else: "the
legal moves from this state" (what the API returns so the frontend renders only buttons that can
succeed) and "the workflow this engagement type follows" (the binding resolution of
DATA_MODEL §2). Return values are materialized lists or model instances — never a `QuerySet` a
caller could extend, which would move the query back out of this file.
"""

from django.db.models import F, Q

from apps.workflow.domain.errors import WorkflowNotConfigured
from apps.workflow.models import Workflow, WorkflowBinding, WorkflowState, WorkflowTransition


def legal_transitions_from(*, from_state_id: int) -> list[WorkflowTransition]:
    """The active edges leaving a state, in the order the operator arranged them.

    This is the query the detail view is built on: the frontend renders one button per row and
    therefore never needs to know which states exist, so adding a state is zero frontend changes.
    Inactive edges are excluded here, not filtered by the caller, because "inactive" means
    "invisible to the transition service" and the two views must not disagree.

    Args:
        from_state_id: `WorkflowState` primary key the aggregate currently sits on.

    Returns:
        The active outgoing transitions with their target state loaded, ordered by `order`.
    """
    return list(
        WorkflowTransition.objects.filter(from_state_id=from_state_id, is_active=True)
        .select_related("to_state", "from_state")
        .order_by("order", "id")
    )


def active_transition(*, from_state_id: int, to_state_code: str) -> WorkflowTransition | None:
    """The one active edge from a state to a target state code, if it exists.

    Returns `None` for both "no such edge" and "no such state code", because the legal set is a
    table: from the caller's side those are the same answer, and the transition service turns
    either into `TransitionNotAllowed` rather than a 500.

    Args:
        from_state_id: `WorkflowState` primary key the aggregate currently sits on.
        to_state_code: `WorkflowState.code`, unique inside the workflow — the target is matched
            within the source state's own workflow, so an identical code in another graph cannot
            be reached by accident.

    Returns:
        The transition with both states loaded, or `None` when the move is not declared.
    """
    return (
        WorkflowTransition.objects.filter(
            from_state_id=from_state_id,
            to_state__code=to_state_code,
            to_state__workflow=F("from_state__workflow"),
            is_active=True,
        )
        .select_related("from_state", "to_state", "workflow")
        .first()
    )


def initial_state(*, workflow_id: int) -> WorkflowState:
    """The entry node of a workflow, used when an aggregate is created.

    Args:
        workflow_id: `Workflow` primary key.

    Returns:
        The single state flagged `is_initial` — the partial unique constraint guarantees at most
        one.

    Raises:
        WorkflowNotConfigured: The workflow has no entry node, so nothing can be created on it.
    """
    state = WorkflowState.objects.filter(workflow_id=workflow_id, is_initial=True).first()
    if state is None:
        raise WorkflowNotConfigured(f"workflow:{workflow_id}")
    return state


def resolve_workflow(*, applies_to: str, engagement_type_id: int | None = None) -> Workflow:
    """The workflow an aggregate of this kind and engagement type follows.

    Resolution order, most specific first: the active binding for that engagement type, then the
    binding whose `engagement_type` is null (the per-kind default binding), then
    `Workflow.is_default`. That order is what lets a Diagnostico run a shorter lifecycle than a
    recurring maintenance engagement by inserting one row.

    Args:
        applies_to: `AppliesTo` value — `PROJECT` or `TASK`.
        engagement_type_id: `catalog.EngagementType` primary key, or `None` to ask only for the
            per-kind default.

    Returns:
        The active workflow to bind the aggregate to.

    Raises:
        WorkflowNotConfigured: Neither a binding nor a default answers for this entity kind.
    """
    bindings = list(
        WorkflowBinding.objects.filter(
            Q(engagement_type_id=engagement_type_id) | Q(engagement_type__isnull=True),
            applies_to=applies_to,
            is_active=True,
            workflow__is_active=True,
        ).select_related("workflow")
    )
    specific = next((b for b in bindings if b.engagement_type_id is not None), None)
    if specific is not None:
        return specific.workflow

    per_kind_default = next((b for b in bindings if b.engagement_type_id is None), None)
    if per_kind_default is not None:
        return per_kind_default.workflow

    default = Workflow.objects.filter(
        applies_to=applies_to, is_default=True, is_active=True
    ).first()
    if default is None:
        raise WorkflowNotConfigured(applies_to)
    return default
