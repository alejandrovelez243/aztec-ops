"""Typed failures of a state change, and of the authoring that shapes the graph it runs on.

Every one of them is a legitimate answer to a legitimate request, never a bug: they are mapped
to HTTP once, centrally (`TransitionNotAllowed` → 409, the rest → 422), so no route ever grows
its own `try/except`. Each carries the identifiers a message needs, because a caller that has to
parse `str(exc)` to render a form error is a caller that will get it wrong.

The file has two halves and they answer two different questions. The first half is **obedience**:
a record proposed a move and the graph refused it. The second half is **authoring**: an operator
proposed a change to the graph itself and the context refused *that*. They are deliberately not
shared — a rejection of a move that reused a rejection of an edit would make one 409 mean two
things — and nothing in the authoring half is ever raised by
:func:`~apps.workflow.services.transition.validate_transition`.
"""


class DomainError(Exception):
    """Base of every typed failure raised by the workflow context."""


class TransitionNotAllowed(DomainError):
    """No active edge exists from the current state to the requested one.

    This is the single rule the whole workflow design exists for. An unknown target code lands
    here too: the legal set is a table, so "no such state" and "not reachable from here" are the
    same answer and neither is a 500.
    """

    def __init__(
        self,
        entity_id: str,
        from_state: str,
        to_state: str,
        allowed: tuple[str, ...] = (),
    ) -> None:
        super().__init__(f"No active transition {from_state} -> {to_state} for {entity_id}.")
        self.entity_id = entity_id
        self.from_state = from_state
        self.to_state = to_state
        #: The target codes that *are* reachable from ``from_state`` right now. Carried on the
        #: error rather than looked up by the HTTP handler, because the handler runs after the
        #: aggregate's transaction closed and would be re-reading a graph the rejection already
        #: knew. `docs/API.md` §1.5 ships it as ``details.allowed`` so a client whose button list
        #: went stale resyncs from the rejection instead of refetching the project.
        self.allowed = allowed


class ReasonRequired(DomainError):
    """The edge sets `requires_reason` and the caller supplied blank text.

    Whitespace does not count. A state change nobody explained is indistinguishable from a
    mistake when the timeline is read three weeks later.
    """

    def __init__(self, entity_id: str, from_state: str, to_state: str) -> None:
        super().__init__(f"Transition {from_state} -> {to_state} on {entity_id} requires a reason.")
        self.entity_id = entity_id
        self.from_state = from_state
        self.to_state = to_state


class RequiredFieldMissing(DomainError):
    """A field named in `requires_fields` is empty on the aggregate.

    Carries the offending field name so the API can disable that one button and say which field
    to fill, instead of letting the operator press it and receive a generic rejection.
    """

    def __init__(self, entity_id: str, field_name: str, to_state: str) -> None:
        super().__init__(
            f"Field '{field_name}' must be set on {entity_id} before moving to {to_state}."
        )
        self.entity_id = entity_id
        self.field_name = field_name
        self.to_state = to_state


class GuardRejected(DomainError):
    """A registered guard refused the move and said why.

    The guard's own sentence is the message: it names the fact that blocked the move, and the API
    surfaces it verbatim.
    """

    def __init__(self, guard: str, entity_id: str, reason: str) -> None:
        super().__init__(f"Guard '{guard}' rejected the transition on {entity_id}: {reason}")
        self.guard = guard
        self.entity_id = entity_id
        self.reason = reason


class GuardNotRegistered(DomainError):
    """`WorkflowTransition.guard` names a callable no module registered.

    Raised rather than ignored: an operator typo in the admin would otherwise silently disable a
    safety check that the edge claims to enforce.
    """

    def __init__(self, guard: str) -> None:
        super().__init__(f"No guard registered under '{guard}'.")
        self.guard = guard


class WorkflowNotConfigured(DomainError):
    """No binding and no default workflow answer for this entity kind.

    The seed is expected to ship one default per kind, so this means the taxonomy was edited into
    an unusable state — loud is the only safe behaviour.
    """

    def __init__(self, applies_to: str) -> None:
        super().__init__(f"No active workflow is bound or default for {applies_to}.")
        self.applies_to = applies_to


# --- Authoring: an operator proposed a change to the graph, and it was refused ------------------


class WorkflowNotFound(DomainError):
    """The graph addressed by ``code`` does not exist.

    A 404, not a 422: the client addressed a resource, and the answer is that there is no such
    resource — which is what tells an editor to stop showing a row somebody removed.
    """

    def __init__(self, workflow_code: str) -> None:
        super().__init__(f"No workflow exists with code {workflow_code!r}.")
        self.workflow_code = workflow_code


class WorkflowStateNotFound(DomainError):
    """The node addressed by ``state_code`` is not in this graph.

    "In another graph" and "nowhere at all" are deliberately the same answer. ``code`` is unique
    only inside its workflow, so a state is always addressed as a pair; a lookup that fell back to
    the global table would happily hand back ``bloqueada`` from a different lifecycle and the edit
    would land on somebody else's graph.
    """

    def __init__(self, workflow_code: str, state_code: str) -> None:
        super().__init__(f"Workflow {workflow_code!r} has no state {state_code!r}.")
        self.workflow_code = workflow_code
        self.state_code = state_code


class TransitionNotFound(DomainError):
    """The edge addressed by its ordered pair of state codes is not in this graph."""

    def __init__(self, workflow_code: str, from_state: str, to_state: str) -> None:
        super().__init__(
            f"Workflow {workflow_code!r} has no transition {from_state} -> {to_state}."
        )
        self.workflow_code = workflow_code
        self.from_state = from_state
        self.to_state = to_state


class DuplicateWorkflowCode(DomainError):
    """A graph was created under a code some other graph already carries.

    A conflict rather than a validation failure: the request is well formed and the operator has to
    pick another slug. It is also the answer for a **retired** graph holding that code — the row
    still exists, aggregates still sit on its states, and the fix is to reactivate it rather than
    to create a second graph that would resolve ambiguously ever after.
    """

    def __init__(self, workflow_code: str) -> None:
        super().__init__(f"A workflow already exists with code {workflow_code!r}.")
        self.workflow_code = workflow_code


class DuplicateStateCode(DomainError):
    """A node was added under a code the graph already contains, retired or not.

    Same reasoning as :class:`DuplicateWorkflowCode`, one level down: the unique constraint is
    ``(workflow, code)``, so restoring the existing node is the fix and a second row is not
    available as one.
    """

    def __init__(self, workflow_code: str, state_code: str) -> None:
        super().__init__(f"Workflow {workflow_code!r} already has a state {state_code!r}.")
        self.workflow_code = workflow_code
        self.state_code = state_code


class DuplicateTransition(DomainError):
    """An edge was declared between two nodes that already have one, in that direction.

    ``(from_state, to_state)`` is unique by constraint, and re-enabling a withdrawn edge is
    ``is_active = True`` rather than a second row — otherwise the graph would hold two rows for one
    move and the transition service would have to decide which of them it obeys.
    """

    def __init__(self, workflow_code: str, from_state: str, to_state: str) -> None:
        super().__init__(f"Workflow {workflow_code!r} already declares {from_state} -> {to_state}.")
        self.workflow_code = workflow_code
        self.from_state = from_state
        self.to_state = to_state


class WorkflowStateInUse(DomainError):
    """A node was asked to leave the graph while records are still sitting on it.

    The whole reason nothing here is ever deleted. ``WorkflowState`` is ``PROTECT``ed by every
    project and task that points at it, so a delete would raise a database error naming a
    constraint; and a *retirement* — ``is_active = False`` — would leave those records parked on a
    node the graph no longer contains, with no column to draw them in and no move to make. So the
    counts travel on the error: an operator who is told "3 proyectos y 1 tarea" knows what to move
    before trying again, and an operator told "that failed" does not.

    Attributes:
        workflow_code: The graph the node belongs to.
        state_code: The node that was asked to retire.
        projects: How many ``portfolio.Project`` rows currently sit on it.
        tasks: How many ``work.Task`` rows currently sit on it.
    """

    def __init__(self, workflow_code: str, state_code: str, *, projects: int, tasks: int) -> None:
        super().__init__(
            f"State {state_code!r} of workflow {workflow_code!r} still holds "
            f"{projects} project(s) and {tasks} task(s)."
        )
        self.workflow_code = workflow_code
        self.state_code = state_code
        self.projects = projects
        self.tasks = tasks

    @property
    def records(self) -> int:
        """How many records in total block the retirement. Zero can never reach this class."""
        return self.projects + self.tasks


class EngagementTypeAlreadyBound(DomainError):
    """An engagement type was bound to a graph while another graph already claims it.

    ``(applies_to, engagement_type)`` is unique, and that is the point: resolution has to be
    deterministic, so an engagement type names exactly one lifecycle per entity kind. Rebinding is
    an edit of the existing binding — an explicit decision — never a second row that would make
    ``Workflow.objects.resolve`` pick by insertion order.
    """

    def __init__(self, engagement_type_code: str, workflow_code: str, applies_to: str) -> None:
        super().__init__(
            f"Engagement type {engagement_type_code!r} is already bound to workflow "
            f"{workflow_code!r} for {applies_to}."
        )
        self.engagement_type_code = engagement_type_code
        self.workflow_code = workflow_code
        self.applies_to = applies_to


class EngagementTypeNotFound(DomainError):
    """A binding named an engagement type the catalog does not carry.

    Raised instead of creating the type: the catalog is a different context's vocabulary, and a
    lifecycle editor that could invent engagement types would be a second, competing definition of
    what the portfolio is (`docs/API.md` §2.16).
    """

    def __init__(self, engagement_type_code: str) -> None:
        super().__init__(f"No engagement type exists with code {engagement_type_code!r}.")
        self.engagement_type_code = engagement_type_code


class ValueOutsideVocabulary(DomainError):
    """A structural vocabulary value was proposed that this system does not define.

    Two fields use it — the entity kind a graph governs (``PROJECT`` | ``TASK``) and a state's
    ``category`` — and both are closed sets *in code*, not operator data: every risk specification,
    every priority signal and every snapshot count branches on ``category``, so a sixth value would
    be a silent no-op rather than a new behaviour (DATA_MODEL §12). One class rather than one per
    field, because the answer a form needs is identical — which field, and what may go in it — and
    a second class would only duplicate the renderer.

    Attributes:
        field: Name of the offending request field, so the UI can point at one input.
        value: What was sent.
        allowed: Every value that would have been accepted.
    """

    def __init__(self, field: str, value: str, allowed: tuple[str, ...]) -> None:
        super().__init__(f"{value!r} is not a valid {field}; expected one of {', '.join(allowed)}.")
        self.field = field
        self.value = value
        self.allowed = allowed
