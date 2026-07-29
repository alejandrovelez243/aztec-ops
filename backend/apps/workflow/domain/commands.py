"""What a caller asks the workflow context to change about a graph.

Authoring, not obedience. Everything here describes an edit an ops lead makes to a lifecycle —
adding a graph, a node or an edge, renaming one, withdrawing one — and nothing here is ever read
while a record is moving. That separation is the whole safety argument of the authoring surface:
the shape of the graph is decided by these commands, and whether a *record* may follow it is
decided by :func:`~apps.workflow.services.transition.validate_transition`, which re-reads the rows
every time it is asked (CLAUDE.md rule 2).

**Absent means untouched.** The update commands carry optional fields and the services read
``model_fields_set``, so ``{"label": "..."}`` renames a state without silently resetting its colour
to the default. That distinction cannot be expressed by a value, which is why ``None`` is a sentinel
here and never "clear this field".

Pure: no Django import, so a command can be built and asserted on ``SimpleTestCase``. The closed
vocabularies these commands carry as plain strings — ``applies_to``, ``category``, ``guard`` — are
validated by the service against the table and the registry that own them, and the typed rejection
names the field and the values that would have worked.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

#: Length of a graph slug, mirroring ``workflow_workflow.code``.
WORKFLOW_CODE_MAX_LENGTH = 32

#: Length of a graph's display name, mirroring ``workflow_workflow.name``.
WORKFLOW_NAME_MAX_LENGTH = 96

#: Length of a node slug, mirroring ``workflow_workflowstate.code``.
STATE_CODE_MAX_LENGTH = 32

#: Length of an operator-editable Spanish label, mirroring ``workflow_workflowstate.label`` and
#: ``workflow_workflowtransition.label``.
LABEL_MAX_LENGTH = 64

#: Length of a registered guard code, mirroring ``workflow_workflowtransition.guard``.
GUARD_MAX_LENGTH = 64

#: ``#RRGGBB`` or nothing at all. The empty string is how the column spells "the operator set no
#: colour", and it reaches the client as ``null`` so a board falls back to its own neutral token
#: instead of painting a swatch of transparent black.
COLOR_PATTERN = r"^(#[0-9a-fA-F]{6})?$"

#: Upper bound of ``order`` on both nodes and edges: the range of the ``SmallIntegerField`` the
#: column actually is. Rejecting 40000 here is a 422 naming the field; letting it through is a
#: database error raised halfway into somebody's transaction.
ORDER_MAX = 32767


class CreateWorkflowCommand(BaseModel):
    """Add a lifecycle: an empty graph, active, governing one kind of aggregate.

    Empty on purpose. A graph is created and *then* shaped, because the alternative — states and
    edges nested inside the creation payload — makes one request that either succeeds whole or
    leaves the operator re-typing a form, and makes every partial failure a different rollback.

    ``is_default`` is not here. At most one graph per ``applies_to`` is the fallback that
    ``Workflow.objects.resolve`` ends on, and moving it is a decision about what *unbound* work
    follows — never a side effect of adding a lifecycle. It stays in the admin.

    Attributes:
        code: Stable slug; what the API addresses the graph by, permanently.
        name: Display name an operator reads and may fix at any time.
        applies_to: ``PROJECT`` or ``TASK``. Validated against the structural vocabulary by the
            service, which raises
            :class:`~apps.workflow.domain.errors.ValueOutsideVocabulary` naming both.
        engagement_types: ``catalog.EngagementType`` codes this graph is bound to for that kind of
            aggregate. Empty is the common case: the graph is then reachable only as the per-kind
            default or by a binding added later.
    """

    model_config = ConfigDict(frozen=True)

    code: str = Field(min_length=1, max_length=WORKFLOW_CODE_MAX_LENGTH)
    name: str = Field(min_length=1, max_length=WORKFLOW_NAME_MAX_LENGTH)
    applies_to: str = Field(min_length=1, max_length=8)
    engagement_types: tuple[str, ...] = ()


class UpdateWorkflowCommand(BaseModel):
    """Rename a lifecycle, retire it, or put it back in service.

    ``code`` is the address and never a field: it is what every binding, every editor URL and every
    document already published names this graph by.

    ``applies_to`` is not editable either, and that is not an omission. Every state of the graph is
    occupied by aggregates of one kind; flipping the kind would leave projects sitting inside a
    graph that claims to govern tasks, and no migration of the rows would follow.

    Attributes:
        code: Which graph is being edited.
        name: New display name.
        is_active: ``False`` retires the graph — it stops being offered for new work and keeps
            resolving for everything already on it; ``True`` puts it back.
    """

    model_config = ConfigDict(frozen=True)

    code: str = Field(min_length=1, max_length=WORKFLOW_CODE_MAX_LENGTH)
    name: str | None = Field(default=None, min_length=1, max_length=WORKFLOW_NAME_MAX_LENGTH)
    is_active: bool | None = None


class AddStateCommand(BaseModel):
    """Add a node to a graph.

    Attributes:
        workflow_code: The graph to add it to.
        code: Node slug, unique **inside this graph only** — two lifecycles may both own
            ``bloqueada``, which is exactly why nothing in the codebase branches on a state code.
        label: The Spanish an operator reads on the column header.
        category: ``BACKLOG`` | ``IN_PROGRESS`` | ``BLOCKED`` | ``DONE`` | ``CANCELLED``. This is
            what the rest of the product branches on, so it is the one field of a state that is not
            free-form, and the service refuses anything outside the five.
        color: ``#RRGGBB``, or empty for "the operator set none".
        order: Where the column sits. Absent **appends** it after the last node of this graph —
            a new column belongs at the end, which is where a reader looks for the thing that was
            just added, and landing at ``0`` would silently reshuffle a graph somebody arranged.
    """

    model_config = ConfigDict(frozen=True)

    workflow_code: str = Field(min_length=1, max_length=WORKFLOW_CODE_MAX_LENGTH)
    code: str = Field(min_length=1, max_length=STATE_CODE_MAX_LENGTH)
    label: str = Field(min_length=1, max_length=LABEL_MAX_LENGTH)
    category: str = Field(min_length=1, max_length=16)
    color: str = Field(default="", pattern=COLOR_PATTERN)
    order: int | None = Field(default=None, ge=0, le=ORDER_MAX)


class UpdateStateCommand(BaseModel):
    """Reword, recolour, recategorise or move a node. Absent means untouched.

    ``code`` is the address. Every project and task on this node holds its primary key, and every
    payload already published names it by slug; renaming is what ``label`` is for.

    ``is_active`` is typed ``Literal[True]`` and that is not a typo. Restoring a node is always safe
    — nothing can be occupying a node that is out of the graph. *Retiring* one can be refused by
    records this request knows nothing about, and it withdraws every arrow touching the node, so it
    is its own use case behind ``DELETE`` (:mod:`~apps.workflow.services.retire_state`). A field
    that sometimes fails for reasons unrelated to itself is a field a form cannot explain, so the
    type system refuses to accept ``false`` here rather than the service refusing to act on it
    (CLAUDE.md rule 13).

    Attributes:
        workflow_code: The graph the node belongs to.
        code: Which node.
        label: New display text.
        category: New structural category, validated against the five.
        color: New ``#RRGGBB``, or empty to clear it.
        order: New position among the columns.
        is_active: ``True`` puts a retired node back in the graph. Its arrows stay withdrawn — they
            were withdrawn as separate facts and are restored as separate decisions.
    """

    model_config = ConfigDict(frozen=True)

    workflow_code: str = Field(min_length=1, max_length=WORKFLOW_CODE_MAX_LENGTH)
    code: str = Field(min_length=1, max_length=STATE_CODE_MAX_LENGTH)
    label: str | None = Field(default=None, min_length=1, max_length=LABEL_MAX_LENGTH)
    category: str | None = Field(default=None, min_length=1, max_length=16)
    color: str | None = Field(default=None, pattern=COLOR_PATTERN)
    order: int | None = Field(default=None, ge=0, le=ORDER_MAX)
    is_active: Literal[True] | None = None


class AddTransitionCommand(BaseModel):
    """Declare a move between two nodes of the same graph.

    Both endpoints are resolved *inside* ``workflow_code``. An edge that joined two graphs would
    put an aggregate into a state its own lifecycle does not contain, which is why the model's
    ``clean()`` refuses it and why this command cannot even express it.

    Attributes:
        workflow_code: The graph both endpoints belong to.
        from_state: Node slug the move leaves.
        to_state: Node slug the move lands on.
        label: The operator's wording for the move — ``Aprobar``, ``Pedir cambios``. Rendered on a
            button and never compared against.
        requires_reason: The move demands written text, enforced by the transition service.
        requires_fields: Names of **aggregate** attributes (``next_step``) that must be non-empty on
            the record before it may take this move. Not request fields: whether they are filled is
            a fact about a row.
        guard: Code of a registered guard, or empty for none. Validated against
            :func:`~apps.workflow.domain.guards.registered_guard_codes`, because a typo would
            otherwise silently disable a safety check the edge claims to enforce.
        order: Where the button sits among the moves leaving ``from_state``. Absent appends it.
    """

    model_config = ConfigDict(frozen=True)

    workflow_code: str = Field(min_length=1, max_length=WORKFLOW_CODE_MAX_LENGTH)
    from_state: str = Field(min_length=1, max_length=STATE_CODE_MAX_LENGTH)
    to_state: str = Field(min_length=1, max_length=STATE_CODE_MAX_LENGTH)
    label: str = Field(min_length=1, max_length=LABEL_MAX_LENGTH)
    requires_reason: bool = False
    requires_fields: tuple[str, ...] = ()
    guard: str = Field(default="", max_length=GUARD_MAX_LENGTH)
    order: int | None = Field(default=None, ge=0, le=ORDER_MAX)


class UpdateTransitionCommand(BaseModel):
    """Change what a declared move asks for. Absent means untouched.

    The ordered pair is the address: an edge *is* its endpoints, and "repoint this arrow" is
    withdrawing one move and declaring another, which is two decisions and reads as two lines in
    the trail.

    Attributes:
        workflow_code: The graph the edge belongs to.
        from_state: Node slug the move leaves.
        to_state: Node slug the move lands on.
        label: New wording.
        requires_reason: Whether the move now demands written text.
        requires_fields: New list of aggregate attributes that must be filled first. Sent whole,
            because a list edited by patching one entry has no unambiguous spelling.
        guard: New registered guard code, or empty to remove the guard.
        order: New position among the moves leaving ``from_state``.
        is_active: ``True`` re-declares a withdrawn move. ``Literal[True]`` for the same reason as
            :class:`UpdateStateCommand`: withdrawing is ``DELETE``, and restoring is the only
            direction that cannot be refused. Re-enabling the existing row is also the *only* way
            back — a second row for the same ordered pair is forbidden by constraint, and would
            leave the transition service with two rows to obey.
    """

    model_config = ConfigDict(frozen=True)

    workflow_code: str = Field(min_length=1, max_length=WORKFLOW_CODE_MAX_LENGTH)
    from_state: str = Field(min_length=1, max_length=STATE_CODE_MAX_LENGTH)
    to_state: str = Field(min_length=1, max_length=STATE_CODE_MAX_LENGTH)
    label: str | None = Field(default=None, min_length=1, max_length=LABEL_MAX_LENGTH)
    requires_reason: bool | None = None
    requires_fields: tuple[str, ...] | None = None
    guard: str | None = Field(default=None, max_length=GUARD_MAX_LENGTH)
    order: int | None = Field(default=None, ge=0, le=ORDER_MAX)
    is_active: Literal[True] | None = None
