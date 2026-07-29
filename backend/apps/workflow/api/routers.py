"""Route of the workflow context: every state graph, for a board to draw its columns and arrows."""

from django.http import HttpRequest
from ninja import Router

from apps.workflow.domain.views import WorkflowCatalogView
from apps.workflow.services.read_workflows import read_workflows

router = Router(tags=["workflow"])


@router.get("/workflows", response=WorkflowCatalogView, url_name="workflows_read")
def get_workflows(request: HttpRequest) -> WorkflowCatalogView:
    """Return every configured state graph: its states and its edges, as an operator arranged them.

    Each state carries ``code``, ``label``, ``category`` and ``color`` — the shared
    :class:`~apps.shared.refs.StateRef` every read surface already renders — and each graph carries
    the engagement types bound to it, the kind of aggregate it governs, whether it is the fallback
    for that kind, and ``transitions``: every active edge as ``from_state`` / ``to_state`` codes plus
    the operator's ``label``, ``requires_reason`` and ``requires_fields``.

    Authenticated like every other read; the list of routes that opt out is five long and closed
    (`docs/API.md` §1.2).

    One document rather than a route per engagement type, for the same reason ``GET /catalog`` is
    one document: a board draws all of its columns at once. Asking per engagement type would also
    force the client to know which type to ask about before its first request, which means
    reimplementing the binding precedence ``Workflow.objects.resolve`` owns.

    **Why publishing edges here does not reopen the decision that kept them out.** This route
    deliberately shipped without edges so that a client could not compute legality locally and skip
    the per-project and per-task ``transitions`` list. That rule stands, unchanged and unweakened.
    What makes edges safe on *this* document is a real distinction, not a rhetorical one: this
    document describes the graph **as an operator configured it**, while a project's or a task's own
    ``transitions`` says what **that record** may do **right now**. The two can legitimately
    disagree, and the ways they disagree are the whole argument. A configured edge is refused when
    the record is not sitting on its ``from_state``; when a guard rejects it — ``GuardRejected`` is
    in the error mapping (§1.5) precisely because a declared edge can be denied; and when a field
    named in ``requires_fields`` is empty *on that record*, which is a fact about a row, not about
    the graph. **So an edge existing is not a move being legal.** A client that reads an arrow here
    and acts on it without asking the record meets exactly the same typed 409 it met before, now
    carrying ``details.allowed``. Every enforcement path stays where it is: nothing in this route is
    read by ``validate_transition``, which re-reads the rows each time it decides.

    **Nor does it become the global state list §2.17 refuses.** ``GET /catalog`` excludes states
    because a state code is unique only inside its workflow, so a *flat* list invites the client to
    guess. Nothing here is flat: states arrive grouped under the graph that owns them, and each edge
    names its endpoints by code inside that same graph, so ``bloqueada`` in two workflows still
    cannot be confused. A board needs the columns and cannot derive them — columns inferred from the
    states projects happen to occupy cannot represent an empty one, so a workflow whose ``Bloqueado``
    state is unoccupied has no such column, cannot say "nothing is blocked", and cannot accept a card
    dropped into it — and a screen that draws the lifecycle needs the arrows and cannot derive those
    either, since an unused edge is invisible to every record that never took it.
    """
    del request
    return read_workflows()
