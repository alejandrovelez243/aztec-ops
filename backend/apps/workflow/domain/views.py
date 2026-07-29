"""What the workflow context publishes to a read surface: the shape of a graph, and its legal moves.

Two projections that answer two different questions, and the split between them is the point.

:class:`TransitionOption` answers **"what may this aggregate do next"**. It is the load-bearing
projection of the whole product: `docs/API.md` §2.2 makes it the **only** source of transition
buttons, so the frontend holds no list of state codes, guesses no legality and renders exactly what
arrives there — adding ``en_espera_cliente`` from the admin is a row and zero frontend changes
(CLAUDE.md rule 14).

:class:`WorkflowShapeView` answers **"which columns does a board have"**. It carries every state of
a graph, including the ones no aggregate currently occupies, and carries no edges at all. A client
holding it can draw an empty ``Bloqueado`` column and can offer it as a drop target; it still cannot
tell whether dropping there is legal, because that answer is not in this shape. Legality remains
where it already is.

Pure Pydantic over the shared kernel — no Django — so a router, a consumer or a test can build one
without a database.
"""

from pydantic import BaseModel, ConfigDict

from apps.shared.refs import StateRef, TaxonomyRef


class TransitionOption(BaseModel):
    """One edge an operator may take from where the aggregate currently sits.

    ``requires_reason`` and ``requires_fields`` are shipped rather than enforced client-side only:
    the client uses them to ask for the reason *before* the request and to disable a button whose
    field is still empty, and the transition service re-checks both, so a client that ignores them
    gets a typed 422 instead of a silent move.

    ``requires_fields`` names attributes of the aggregate (``next_step``), not of the request body.
    An entry that is currently empty on the aggregate means the button renders disabled with the
    field named — which is why the list is sent even when it changes nothing about the payload.
    """

    model_config = ConfigDict(frozen=True)

    to_state: StateRef
    label: str
    requires_reason: bool = False
    requires_fields: tuple[str, ...] = ()


def required_field_names(requires_fields: object) -> tuple[str, ...]:
    """Read ``WorkflowTransition.requires_fields`` defensively.

    The column is JSONB an operator edits as free-form JSON in the admin, so it can legitimately
    arrive as ``null``, as an object, or with blank entries. A non-list is read as "no required
    fields" rather than raising: a malformed edit must not make an otherwise legal transition
    impossible to take, and the reviewer sees the empty list in the API response.

    Lives here so the transition service and the read projection parse the column identically —
    a button rendered from one parser and validated by another is how a UI ends up offering a move
    the backend then refuses.

    Args:
        requires_fields: The raw JSONB value.

    Returns:
        The non-blank field names, in the order the operator wrote them.
    """
    if not isinstance(requires_fields, list):
        return ()
    return tuple(str(name) for name in requires_fields if str(name).strip())


class WorkflowShapeView(BaseModel):
    """One state graph as a board sees it: its columns, in the operator's order.

    Every state of the graph is here, occupied or not, which is the entire reason this shape
    exists. Deriving the columns from the states projects happen to sit in cannot represent an
    empty one, so a board built that way can neither say "nothing is blocked" nor accept a card
    dropped into ``Bloqueado`` — the column is simply absent.

    What is deliberately **not** here is any edge. This shape says which columns exist; it never
    says which move between them is allowed. That answer stays on the project detail's
    ``transitions`` (`docs/API.md` §2.2), computed against the state the project is actually in,
    against ``is_active`` and against the guard. A client that drags a card into a column it may
    not enter gets a typed 409 carrying the moves it may take — the same failure mode as a stale
    button — instead of a board that quietly disagrees with the backend.

    Attributes:
        applies_to: ``PROJECT`` | ``TASK`` — the kind of aggregate this graph governs. Load-bearing
            for a board: without it a project board would draw the task graph's columns.
        is_default: The fallback graph for its ``applies_to``, which is where
            ``Workflow.objects.resolve`` ends when no binding matches. Descriptive, like
            ``engagement_types``: it lets a board pick a column set, never a legal move.
        is_active: A retired graph is still published, because aggregates keep sitting on its
            states — states are ``PROTECT``ed exactly so — and a board that could not draw their
            columns would lose those projects entirely. The flag is here so a client can stop
            offering it for new work.
        engagement_types: The engagement types whose active binding names this graph. Empty is the
            common case and means no engagement type names it specifically: it is reached as the
            per-kind default. Plural because the binding table permits many types per graph — a
            single nullable field would silently drop rows.
        states: Every node, ordered by the ``order`` an operator arranged in the admin, ``code``
            breaking ties. Empty is a legitimate answer — a graph whose states nobody has
            configured yet — and the board renders no columns rather than treating it as a failure.
    """

    model_config = ConfigDict(frozen=True)

    code: str
    name: str
    applies_to: str
    is_default: bool = False
    is_active: bool = True
    engagement_types: tuple[TaxonomyRef, ...] = ()
    states: tuple[StateRef, ...] = ()


class WorkflowCatalogView(BaseModel):
    """Every configured graph, in one document.

    One document rather than a route per engagement type, for the same reason
    :class:`~apps.catalog.domain.value_objects.CatalogView` is one document: a board draws all of
    its columns at once. A per-engagement-type route would also be the worse shape — the client
    would have to know which engagement type to ask about *before* it could make the first
    request, which means reimplementing the binding precedence that
    ``Workflow.objects.resolve`` owns, and it would fan a single board render into one request per
    engagement type on screen.

    The tables hold single-digit row counts, so serving all of them costs three index scans and
    the client caches one answer instead of N.
    """

    model_config = ConfigDict(frozen=True)

    workflows: tuple[WorkflowShapeView, ...] = ()
