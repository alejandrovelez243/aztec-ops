"""Route of the workflow context: the shape of every state graph, for a board to draw columns."""

from django.http import HttpRequest
from ninja import Router

from apps.workflow.domain.views import WorkflowCatalogView
from apps.workflow.services.read_workflows import read_workflows

router = Router(tags=["workflow"])


@router.get("/workflows", response=WorkflowCatalogView, url_name="workflows_read")
def get_workflows(request: HttpRequest) -> WorkflowCatalogView:
    """Return every configured state graph: its states, in the order an operator arranged them.

    Each state carries ``code``, ``label``, ``category`` and ``color`` — the shared
    :class:`~apps.shared.refs.StateRef` every read surface already renders — and each graph carries
    the engagement types bound to it, the kind of aggregate it governs and whether it is the
    fallback for that kind.

    Authenticated like every other read; the list of routes that opt out is five long and closed
    (`docs/API.md` §1.2).

    One document rather than a route per engagement type, for the same reason ``GET /catalog`` is
    one document: a board draws all of its columns at once. Asking per engagement type would also
    force the client to know which type to ask about before its first request, which means
    reimplementing the binding precedence ``Workflow.objects.resolve`` owns.

    **This is the shape of a workflow, not a global state list, and it does not publish legality.**
    ``GET /catalog`` deliberately excludes states, and its reasoning holds unchanged: a state is
    scoped to its workflow and the legal moves out of one are ``transitions`` on the project detail
    (§2.2), so a *flat* list of states would invite the client to guess which move is allowed.
    Nothing here is flat — states arrive grouped under the graph that owns them and no edge is
    published at all, so the only question this answers is which columns exist. A board needs that
    and cannot derive it: columns inferred from the states projects happen to occupy cannot
    represent an empty one, so a workflow whose ``Bloqueado`` state is unoccupied has no such
    column, cannot say "nothing is blocked", and cannot accept a card dropped into it. Whether that
    drop is legal is still answered only by ``transitions``, and a card dragged somewhere it may not
    go gets the typed 409 that carries the moves it may take (§1.5).
    """
    del request
    return read_workflows()
