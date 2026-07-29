"""Use case: serve every configured state graph, so a board can draw its columns and its arrows."""

from apps.workflow.domain.views import WorkflowCatalogView
from apps.workflow.models import Workflow


def read_workflows() -> WorkflowCatalogView:
    """Read every configured graph with its states and its edges, as an operator arranged them.

    Four index scans — the graphs, their nodes, their active edges, their engagement-type bindings —
    and nothing else, because :meth:`~apps.workflow.models.WorkflowQuerySet.with_shape` prefetches
    what each projection reads. The tables hold single-digit row counts, so the document is cheap
    enough to fetch on board load and simple enough that the client never caches it defensively.

    Retired graphs are served too, and this is the deliberate difference from
    :func:`~apps.catalog.services.read_catalog.read_catalog`, which publishes only active rows. The
    catalog feeds *pickers*, where offering a retired value would let an operator choose it and
    quietly un-retire it. Nothing here is chooseable: this is the shape of a graph, and aggregates
    keep sitting on the states of a retired one — they are ``PROTECT``ed exactly so — which means a
    board that could not draw their columns would lose those projects from the screen entirely.
    Each shape carries ``is_active`` so a client can still refuse to offer it for new work.

    This does **not** publish legality, and it does not become a global state list: the states
    arrive grouped under the graph that owns them and the edges name their endpoints by code inside
    that same graph, so the collision that makes a flat list unsafe — ``bloqueada`` existing in two
    workflows — still cannot happen. What the edges answer is "what did the operator configure",
    which is a fact about the admin. "May this record move there now" is a fact about a record, it
    depends on the state that record is on, on its own fields and on the guard, and it stays on
    ``transitions`` in the project or task detail (`docs/API.md` §2.2). This function reads; it
    shares no code path with :func:`~apps.workflow.services.transition.validate_transition`, which
    re-reads the rows every time it decides.

    Returns:
        Every graph as an immutable
        :class:`~apps.workflow.domain.views.WorkflowCatalogView`, safe to serialize with no further
        database access.
    """
    return WorkflowCatalogView(
        workflows=tuple(workflow.to_shape() for workflow in Workflow.objects.with_shape()),
    )
