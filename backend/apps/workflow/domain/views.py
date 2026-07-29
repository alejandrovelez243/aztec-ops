"""What the workflow context publishes to a read surface: the shape of a graph, and its legal moves.

Three projections answering two different questions, and the split between the questions is the
point.

:class:`TransitionOption` answers **"what may this aggregate do next"**. It is the load-bearing
projection of the whole product: `docs/API.md` §2.2 makes it the **only** source of transition
buttons, so the frontend holds no list of state codes, guesses no legality and renders exactly what
arrives there — adding ``en_espera_cliente`` from the admin is a row and zero frontend changes
(CLAUDE.md rule 14).

:class:`WorkflowShapeView` and :class:`WorkflowEdgeView` answer **"what did the operator
configure"**. Together they are the graph as it stands in the admin: every node, occupied or not,
and every edge an operator declared between them. That is a description of configuration, and it is
not a permission. A configured edge can still be refused for the record in front of you — a guard
rejects it (:class:`~apps.workflow.domain.errors.GuardRejected`), a required field on *that* record
is empty, the record is not on the edge's source state at all — so "an edge exists" and "this move
is legal now" are different claims and only the second one is answered by ``transitions`` on a
project or task detail. Publishing the graph therefore takes nothing away from the enforcement path
and adds nothing to the client's authority: a client that acts on an edge alone still meets the
same typed 409.

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


class WorkflowEdgeView(BaseModel):
    """One edge an operator declared between two states of the same graph.

    **This is configuration, not permission.** It says the operator drew this arrow; it does not say
    the record you are looking at may follow it. Three things stand between the two claims, and none
    of them is knowable from this shape: the record has to be sitting on ``from_state`` to begin
    with, the fields named in ``requires_fields`` have to be non-empty *on that record*, and the
    edge's guard — if it names one — has to accept it, which is a question about facts the record's
    own context supplies. So a graph drawn from these edges is a picture of the workflow, and
    ``transitions`` on the project or task detail (`docs/API.md` §2.2) remains the only answer to
    "may this record move there now".

    Attributes:
        from_state: ``WorkflowState.code`` of the source node.
        to_state: ``WorkflowState.code`` of the target node.
        label: The operator's own wording for the move — ``Aprobar``, ``Pedir cambios``. Rendered as
            the arrow's caption; never compared against.
        requires_reason: The operator marked this move as needing a written reason. Descriptive
            here — the transition service re-checks it and raises ``ReasonRequired`` — but it is
            what lets a diagram mark which arrows will ask for text before they are taken.
        requires_fields: Names of aggregate attributes (``next_step``) the operator declared must be
            non-empty before this move. Whether they *are* empty is a fact about a record, so this
            shape lists the requirement and never its outcome.

    Notes:
        The endpoints are codes rather than :class:`~apps.shared.refs.StateRef` values, unlike
        :class:`TransitionOption`. An edge is a relation between two nodes the same document already
        publishes in full under ``states``: repeating the label, category and color on both ends of
        every arrow would let a client render an edge without ever resolving its nodes, and the day
        an operator renames a state the two copies disagree inside one response. ``TransitionOption``
        embeds the full reference for the opposite reason — its consumer holds no node list, only the
        record.

        The ``guard`` name is omitted, exactly as it is on :class:`TransitionOption`. Whether a guard
        passes depends on facts no row in this graph holds, so naming it would invite a client to
        predict the answer — which is the one thing this projection must not enable.
    """

    model_config = ConfigDict(frozen=True)

    from_state: str
    to_state: str
    label: str
    requires_reason: bool = False
    requires_fields: tuple[str, ...] = ()


class WorkflowShapeView(BaseModel):
    """One state graph as a board sees it: its columns, in the operator's order.

    Every state of the graph is here, occupied or not, which is the entire reason this shape
    exists. Deriving the columns from the states projects happen to sit in cannot represent an
    empty one, so a board built that way can neither say "nothing is blocked" nor accept a card
    dropped into ``Bloqueado`` — the column is simply absent.

    ``transitions`` carries the arrows between those columns, and it is a description of what an
    operator configured — see :class:`WorkflowEdgeView`. What this shape still never says is which
    move a given record may make: that is computed per project and per task against the state it is
    actually in, against ``is_active`` and against the guard, and it stays on the detail's own
    ``transitions`` (`docs/API.md` §2.2). A client that acts on an edge published here without
    asking gets a typed 409 carrying the moves the record may actually take — the same failure mode
    as a stale button — so the graph is drawable and the enforcement path is untouched.

    Nodes and edges are siblings here rather than the edges hanging off the state they leave. A
    graph is a pair of sets, and an arrow belongs to neither endpoint: nesting the moves under
    ``from_state`` would make "what leads *into* ``bloqueada``" a scan of every node, and — the
    deciding reason — it would put a key named ``transitions`` on a state object, one nesting level
    away from the per-record ``transitions`` that means "legal right now". Two lists with the same
    name and different authority is exactly the confusion this document must not create. As
    siblings they are also structurally different — an edge names both of its endpoints, an option
    names only a target — so no client can feed one where the other is expected.

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
        transitions: Every **active** edge of the graph, grouped by the source node in the same
            column order as ``states`` and then by the operator's own ``order`` within it. A
            deactivated edge is absent rather than flagged: ``is_active = False`` is how an operator
            withdraws a move, the transition service refuses it, and publishing it as a drawable
            arrow would advertise a move nothing can take. Empty is legitimate and means the
            operator has declared no move yet — a graph of isolated columns, which is a real
            configuration and not a failure.
    """

    model_config = ConfigDict(frozen=True)

    code: str
    name: str
    applies_to: str
    is_default: bool = False
    is_active: bool = True
    engagement_types: tuple[TaxonomyRef, ...] = ()
    states: tuple[StateRef, ...] = ()
    transitions: tuple[WorkflowEdgeView, ...] = ()


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
