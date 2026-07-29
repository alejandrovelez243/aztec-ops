"""Typed failures of a state change.

Every one of them is a legitimate answer to a legitimate request, never a bug: they are mapped
to HTTP once, centrally (`TransitionNotAllowed` → 409, the rest → 422), so no route ever grows
its own `try/except`. Each carries the identifiers a message needs, because a caller that has to
parse `str(exc)` to render a form error is a caller that will get it wrong.
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
