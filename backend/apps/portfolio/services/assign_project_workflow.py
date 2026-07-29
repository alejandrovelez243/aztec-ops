"""Use case: decide which lifecycle one project follows, or hand it back to its engagement type."""

from datetime import datetime
from uuid import UUID

from django.db import transaction

from apps.activity.domain.value_objects import ActivityCommand
from apps.activity.models import ActivityRecord
from apps.activity.services import write_activity
from apps.events.domain.envelope import ENTITY_PROJECT, TOPIC_PROJECT_UPDATED
from apps.events.services import enqueue_event
from apps.portfolio.domain.errors import ProjectNotFound
from apps.portfolio.domain.value_objects import ProjectResult
from apps.portfolio.models import Project
from apps.workflow.domain.errors import WorkflowNotFound
from apps.workflow.models import AppliesTo, Workflow
from apps.workflow.services.reassignment import validate_reassignment


@transaction.atomic
def assign_project_workflow(
    *,
    project_code: str,
    workflow_code: str | None,
    actor: str,
    correlation_id: UUID,
    now: datetime,
) -> ProjectResult:
    """Put a project on a named lifecycle, or clear the assignment so it inherits one again.

    **What this is, and what it deliberately is not.** It changes which graph governs the project.
    It does not move the project: the state ``code`` it stands on is the same before and after, and
    the project is simply repointed at the equivalent node of the target graph. That is what keeps
    CLAUDE.md rule 2 intact while this route exists — a state change still goes through
    ``transition_project`` and still validates against ``WorkflowTransition``, and this operation
    cannot be used to reach a state no edge leads to.

    **The failure mode, stated plainly.** When the target graph has no active state carrying the
    project's current state code, this refuses with
    :class:`~apps.workflow.domain.errors.IncompatibleWorkflowState` — a 409 naming the project, the
    state it stands on, both graphs and the states the target does offer. The alternative — letting
    the caller name a landing state in the target — was rejected: it would make this route a second
    writer of ``workflow_state`` with no edge, no guard and no required-field check behind it, which
    is exactly the door rule 2 closes. ``apps.workflow.services.reassignment`` argues it in full.
    A silent reassignment is not among the options: a project left standing on a state its own
    workflow does not contain is a corrupted aggregate, so this either lands it on a state of the
    new graph or changes nothing at all.

    ``workflow_code=None`` clears the assignment. The project then follows whatever the binding
    ladder hands it — its engagement type's binding, the per-kind default binding, or the default
    graph — and that resolved graph is checked for compatibility exactly like a named one, because
    "inherited" is not a synonym for "safe".

    An assignment that changes nothing — the same graph is already pinned, or there was no
    assignment and none is being made — returns the current projection without an ``ActivityRecord``
    and without an ``OutboxEvent``, for the reason ``update_project`` gives: a no-op that emits an
    event teaches consumers to recompute for nothing.

    Args:
        project_code: Business code, e.g. ``PRJ-01``.
        workflow_code: ``Workflow.code`` to follow, or ``None`` to go back to inheriting one.
        actor: ``accounts.User.code`` of the ops lead making the decision.
        correlation_id: Threaded from the API boundary.
        now: Domain time, supplied by the caller so a replay is deterministic.

    Returns:
        The project as it stands after the reassignment.

    Raises:
        ProjectNotFound: No project carries ``project_code``.
        WorkflowNotFound: ``workflow_code`` matches no graph.
        WorkflowKindMismatch: The named graph governs tasks, not projects.
        WorkflowRetired: The named graph is out of service and takes no arrivals.
        IncompatibleWorkflowState: The target has no active state matching the project's current
            one.
        WorkflowNotConfigured: ``workflow_code`` was ``None`` and no binding or default answers for
            projects.
    """
    project = Project.objects.locked().with_relations().by_code(project_code).first()
    if project is None:
        raise ProjectNotFound(project_code)

    requested = _workflow_named(workflow_code)
    if project.workflow_id == (requested.pk if requested is not None else None):
        return project.to_result()

    # One door for the precedence: the record's own graph first, then the bindings, then the
    # per-kind default. Passing ``requested`` through ``resolve`` rather than around it is what
    # makes "assign this graph" and "go back to inheriting" the same call.
    target = Workflow.objects.resolve(
        applies_to=AppliesTo.PROJECT,
        engagement_type_id=project.engagement_type_id,
        assigned=requested,
    )
    check = validate_reassignment(entity=project, target=target, applies_to=AppliesTo.PROJECT)

    project.workflow = requested
    project.workflow_state_id = check.landing_state_id
    project.save(update_fields=["workflow", "workflow_state", "updated_at"])

    write_activity(
        ActivityCommand(
            entity_type=ENTITY_PROJECT,
            entity_id=project.code,
            verb=ActivityRecord.Verb.WORKFLOW_ASSIGNED,
            actor=actor,
            from_value=check.from_workflow_code,
            to_value=check.to_workflow_code,
            metadata={
                # Both graphs are already in ``from_value``/``to_value``; ``source`` is the fact
                # neither of them carries — whether this record now names its own lifecycle or was
                # handed back to its engagement type's.
                "source": "DIRECT" if requested is not None else "INHERITED",
                "state": check.state_code,
            },
            occurred_at=now,
            correlation_id=correlation_id,
        )
    )
    # EVENTS.md §4 ``project.updated``: the same changes map every other project edit publishes,
    # keyed by model field name. ``workflow_state`` is absent on purpose — the state code did not
    # move, and naming a field whose value is identical on both sides would have consumers diffing
    # a change that did not happen.
    enqueue_event(
        topic=TOPIC_PROJECT_UPDATED,
        entity_type=ENTITY_PROJECT,
        entity_id=project.code,
        payload={
            "changes": {
                "workflow": {
                    "from": check.from_workflow_code,
                    "to": check.to_workflow_code,
                }
            }
        },
        actor=actor,
        correlation_id=correlation_id,
        occurred_at=now,
    )

    # Re-read so the projection carries the landing state's own row rather than the stale related
    # object cached on the instance from before the assignment.
    reassigned = Project.objects.with_relations().by_code(project_code).first()
    if reassigned is None:  # pragma: no cover - the row is locked inside this transaction
        raise ProjectNotFound(project_code)
    return reassigned.to_result()


def _workflow_named(workflow_code: str | None) -> Workflow | None:
    """Resolve the graph the caller named, or ``None`` when they named none.

    ``None`` in and ``None`` out is the "clear the assignment" instruction travelling as the value
    the column holds, rather than as a second boolean parameter that would let a caller ask for both
    at once.

    Args:
        workflow_code: ``Workflow.code``, or ``None``.

    Returns:
        The graph, or ``None``.

    Raises:
        WorkflowNotFound: A code was given and no graph carries it. Retired graphs resolve here and
            are refused later, by name, rather than being reported as missing.
    """
    if workflow_code is None:
        return None
    workflow = Workflow.objects.filter(code=workflow_code).first()
    if workflow is None:
        raise WorkflowNotFound(workflow_code)
    return workflow
