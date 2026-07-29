"""How many records are standing on a state — the second query in this codebase with no model.

Every other named query in the workflow context lives on its model's ``QuerySet``:
``WorkflowTransition.objects.active().from_state(id)`` is the legal-move set,
``WorkflowState.objects.for_workflow(id).next_order()`` is where a new column goes. This one cannot
be, and the reason is the same one that put owner load in ``portfolio.repositories`` — the only
other module of this kind in the repository (`ARCHITECTURE.md` §3.4):

* **It has two owners and no home.** The rows are ``portfolio.Project`` and ``work.Task``; the
  question is about a ``workflow.WorkflowState``. No single model's manager can answer it without
  learning that the other two contexts exist.
* **It cannot live where the rows are.** On ``portfolio`` it would be a workflow question inside the
  context that merely happens to store one of the two answers, and ``work`` would need its own copy;
  the two copies would then disagree the day a third context starts pointing at a state.
* **A counter on the state would be worse.** A denormalized ``record_count`` column is a projection
  that drifts silently — the same trap the source ``Team`` sheet's own counters are (DATA_MODEL §3)
  — and it would have to be maintained by every transition, every create and every archive.

So it lives in the context that *consumes* the answer. ``workflow`` is allowed to know both halves;
neither half is allowed to know it is being counted. Nothing here writes.

Two callers, one definition. The retirement use case needs the count to refuse
(:class:`~apps.workflow.domain.errors.WorkflowStateInUse` reports it), and the graph document needs
it to publish ``can_retire`` — and if those two came from different queries, the editor would
eventually offer a button the API refuses.
"""

from collections.abc import Sequence

from django.db.models import Count

from apps.portfolio.models import Project
from apps.work.models import Task
from apps.workflow.domain.value_objects import StateOccupancy


def records_on_states(state_ids: Sequence[int]) -> dict[int, StateOccupancy]:
    """Count the projects and the tasks sitting on each of the given states.

    Two aggregate queries whatever the number of states, because a per-state count is how a board
    with eight columns becomes sixteen round trips. States nobody occupies are **absent** from the
    result rather than mapped to a zero: the caller reads it with ``.get(pk)`` and treats a miss as
    empty, which is the same answer without paying to write it down.

    Archived projects are counted. They still point at the state — ``PROTECT``ed exactly so — and
    retiring the node under them would leave rows referring to a column that no longer exists;
    "archived" means out of the board's way, not out of the graph.

    Args:
        state_ids: ``WorkflowState`` primary keys to count. Empty returns ``{}`` without querying.

    Returns:
        Occupancy keyed by state primary key, carrying only the states at least one record sits on.
    """
    if not state_ids:
        return {}

    projects: dict[int, int] = dict(
        Project.objects.filter(workflow_state_id__in=state_ids)
        .values_list("workflow_state_id")
        .annotate(total=Count("id"))
    )
    tasks: dict[int, int] = dict(
        Task.objects.filter(workflow_state_id__in=state_ids)
        .values_list("workflow_state_id")
        .annotate(total=Count("id"))
    )
    return {
        state_id: StateOccupancy(
            projects=projects.get(state_id, 0),
            tasks=tasks.get(state_id, 0),
        )
        for state_id in set(projects) | set(tasks)
    }


def record_counts_on_states(state_ids: Sequence[int]) -> dict[int, int]:
    """The same counts, flattened to a total per state, for the graph projection.

    :meth:`~apps.workflow.models.Workflow.to_shape` publishes one number per node — an editor says
    "3 registros en este estado", and splitting projects from tasks on a column header is detail a
    board cannot use. The refusal path keeps the breakdown, because an operator who has to *move*
    those records needs to know where to look.

    Args:
        state_ids: ``WorkflowState`` primary keys to count.

    Returns:
        Total records per state primary key, carrying only the occupied states.
    """
    return {
        state_id: occupancy.total for state_id, occupancy in records_on_states(state_ids).items()
    }
