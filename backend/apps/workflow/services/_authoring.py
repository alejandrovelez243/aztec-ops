"""What the eight authoring use cases share: how a graph is addressed, checked and reported.

Private to ``services/``. Nothing here is a use case — there is no transaction, no write of its own
except the trail entry the caller hands it — and that is deliberate: the invariants live in the use
cases, and this module only makes sure the eight of them address a graph the same way, refuse the
same values with the same typed errors, and answer with the same document.

Three jobs, and the reason each is here rather than repeated eight times:

* **Resolution.** A state is addressed by ``(workflow, code)`` and never by ``code`` alone, because
  a state code is unique only inside its graph — a global lookup would happily hand back
  ``bloqueada`` from another lifecycle and the edit would land on somebody else's board. Writing
  that pair lookup once is what makes "an endpoint in another workflow" impossible to express
  rather than merely rejected.
* **Vocabulary checks.** ``applies_to``, ``category`` and ``guard`` are closed sets owned by a
  ``TextChoices`` and a registry. Checked in one place, they produce one typed rejection naming the
  field and the values that would have worked; checked per use case, they eventually produce three
  spellings of the same 422.
* **The answer.** Every write returns the whole graph as ``GET /api/v1/workflows`` publishes it, so
  an editor re-renders from the document it already knows how to read instead of patching its local
  copy from six differently-shaped responses.
"""

from collections.abc import Sequence
from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, JsonValue

from apps.activity.domain.value_objects import ActivityCommand
from apps.catalog.models import EngagementType
from apps.workflow.domain.errors import (
    EngagementTypeNotFound,
    TransitionNotFound,
    ValueOutsideVocabulary,
    WorkflowNotFound,
    WorkflowStateNotFound,
)
from apps.workflow.domain.guards import resolve_guard
from apps.workflow.domain.views import WorkflowShapeView
from apps.workflow.models import (
    AppliesTo,
    StateCategory,
    Workflow,
    WorkflowState,
    WorkflowTransition,
)
from apps.workflow.repositories import record_counts_on_states

#: ``entity_type`` of every trail entry the authoring use cases write. States and edges are recorded
#: under the graph that owns them: a state ``code`` is unique only inside its workflow, so it is not
#: an identifier the trail could address on its own.
WORKFLOW_ENTITY = "workflow"

#: ``FieldChange.field`` of a rename. Named once because two use cases look for it to decide what
#: ``from_value`` / ``to_value`` carry, and a string typed twice is a string that will differ once.
LABEL_FIELD = "label"


class FieldChange(BaseModel):
    """One field an edit actually moved, before and after, as text.

    Text on both sides even for a number or a flag, and deliberately so: ``ActivityRecord`` stores
    ``from_value`` and ``to_value`` as characters because a trail is read, not recomputed, and a
    metadata map that disagreed with the columns beside it would be worse than one that repeats them.

    Collected rather than written immediately, so a use case can decide *after* looking at
    everything whether anything moved at all — an edit that changed nothing writes no entry, and a
    trail that recorded non-changes could not be read for changes.
    """

    model_config = ConfigDict(frozen=True)

    field: str
    before: str
    after: str


def changed_fields(changes: Sequence[FieldChange]) -> dict[str, JsonValue]:
    """Render the collected changes as the ``metadata.changed`` map of a trail entry.

    ``{"category": {"from": "BACKLOG", "to": "DONE"}}``: keyed by field so a reader can ask "when did
    this column change category" without parsing prose, and holding both sides because "it changed"
    is not a fact anybody can act on.

    Args:
        changes: What the use case decided actually moved.

    Returns:
        The map, ready to hand to :func:`authoring_record`.
    """
    return {change.field: {"from": change.before, "to": change.after} for change in changes}


def renaming(changes: Sequence[FieldChange]) -> FieldChange | None:
    """The label change among the collected ones, if the edit contained one.

    A rename is what a timeline row is read for, so it is what ``from_value`` / ``to_value`` carry;
    an edit that only moved a colour leaves both empty rather than repeating one unchanged label on
    both sides as if something had happened to it.

    Args:
        changes: What the use case decided actually moved.

    Returns:
        The label change, or ``None`` when the wording did not move.
    """
    return next((change for change in changes if change.field == LABEL_FIELD), None)


def locked_workflow(code: str) -> Workflow:
    """Load the graph an authoring use case is about to change, and hold its row.

    The lock is the concurrency story of the whole surface. ``order`` is computed by reading the
    current maximum, and two operators appending a column at the same instant would otherwise both
    read 3 and both write 4 — an arrangement no unique constraint defends. Locking the *workflow*
    rather than the state or the edge is what also serialises "add a node" against "add an arrow
    into that node".

    Args:
        code: ``Workflow.code``.

    Returns:
        The locked row. The caller is already inside ``transaction.atomic``.

    Raises:
        WorkflowNotFound: No graph carries that code.
    """
    workflow = Workflow.objects.locked().filter(code=code).first()
    if workflow is None:
        raise WorkflowNotFound(code)
    return workflow


def state_of(workflow: Workflow, code: str) -> WorkflowState:
    """Resolve one node **inside** a graph.

    The pair is the identity. Resolving a state by ``code`` alone is the bug this function exists to
    make unwritable: two lifecycles may both own ``bloqueada``, so a global lookup would let an edge
    join two graphs and put an aggregate into a state its own lifecycle does not contain.

    Args:
        workflow: The graph the node must belong to.
        code: ``WorkflowState.code`` inside that graph.

    Returns:
        The node.

    Raises:
        WorkflowStateNotFound: The graph has no such node — which is also the answer when the code
            names a node of a different graph.
    """
    state = WorkflowState.objects.for_workflow(workflow.pk).filter(code=code).first()
    if state is None:
        raise WorkflowStateNotFound(workflow.code, code)
    return state


def transition_of(workflow: Workflow, from_state: str, to_state: str) -> WorkflowTransition:
    """Resolve one edge by the ordered pair of state codes that identifies it.

    ``WorkflowTransition`` has no ``code`` column: an edge *is* its endpoints (DATA_MODEL §2), and
    the unique constraint on the pair is what keeps that identity honest.

    Args:
        workflow: The graph the edge belongs to.
        from_state: Source node code.
        to_state: Target node code.

    Returns:
        The edge, retired or not — withdrawing one twice must answer "already withdrawn", not
        "no such edge".

    Raises:
        TransitionNotFound: The graph declares no such move.
    """
    transition = (
        WorkflowTransition.objects.for_workflow(workflow.pk)
        .with_states()
        .filter(from_state__code=from_state, to_state__code=to_state)
        .first()
    )
    if transition is None:
        raise TransitionNotFound(workflow.code, from_state, to_state)
    return transition


def engagement_type_of(code: str) -> EngagementType:
    """Resolve a catalog engagement type a binding names.

    A primary-key-equivalent lookup with no join and no predicate, so it earns no named query
    (PATTERNS §2); what it earns is one place that turns "no such row" into the right typed error.

    Args:
        code: ``catalog.EngagementType.code``.

    Returns:
        The engagement type row.

    Raises:
        EngagementTypeNotFound: The catalog carries no such type. Never created here: a lifecycle
            editor that could invent engagement types would be a second definition of what the
            portfolio is.
    """
    engagement_type = EngagementType.objects.filter(code=code).first()
    if engagement_type is None:
        raise EngagementTypeNotFound(code)
    return engagement_type


def checked_applies_to(value: str) -> str:
    """Return the entity kind, or refuse it naming the two that exist.

    Args:
        value: Proposed ``Workflow.applies_to``.

    Returns:
        The value, unchanged, once it is known to be one of ``AppliesTo``.

    Raises:
        ValueOutsideVocabulary: Anything else. A third kind of aggregate would be a code change in
            every context that reads a workflow, not a row an operator types.
    """
    if value not in AppliesTo.values:
        raise ValueOutsideVocabulary("applies_to", value, tuple(AppliesTo.values))
    return value


def checked_category(value: str) -> str:
    """Return the state category, or refuse it naming the five that exist.

    Args:
        value: Proposed ``WorkflowState.category``.

    Returns:
        The value, unchanged, once it is known to be one of ``StateCategory``.

    Raises:
        ValueOutsideVocabulary: Anything else. ``category`` is what every risk specification, every
            priority signal and every snapshot count branches on, so a sixth value would be a
            silently inert state rather than a new behaviour (DATA_MODEL §12).
    """
    if value not in StateCategory.values:
        raise ValueOutsideVocabulary("category", value, tuple(StateCategory.values))
    return value


def checked_guard(value: str) -> str:
    """Return the guard code, or refuse it naming the registered ones.

    The empty string is "no guard" and is returned untouched — that is how an operator removes one.

    Args:
        value: Proposed ``WorkflowTransition.guard``.

    Returns:
        The value, unchanged, once it resolves in the registry.

    Raises:
        GuardNotRegistered: Nothing is registered under that code. Checked at *authoring* time and
            not only when a record moves, because an edge that names a typo would otherwise sit in
            the graph claiming to enforce a safety check that evaporated (CLAUDE.md rule 8).
    """
    if not value:
        return value
    resolve_guard(value)
    return value


def shape_of(workflow: Workflow) -> WorkflowShapeView:
    """Re-read the graph a use case just changed, as ``GET /api/v1/workflows`` publishes it.

    Every write answers with the whole graph rather than with the row it touched. An editor that
    added a state gets back the arrangement including the state it did not send an ``order`` for,
    the counts that decide which nodes can now be retired, and the arrows the change withdrew — none
    of which it could have derived from an echo of its own request.

    Re-reads through :meth:`~apps.workflow.models.WorkflowQuerySet.with_shape` rather than reusing
    the instances the use case mutated, so what comes back is what the next ``GET`` will say.

    Args:
        workflow: The graph that was changed.

    Returns:
        The graph as an immutable
        :class:`~apps.workflow.domain.views.WorkflowShapeView`, safe to serialize with no further
        database access.
    """
    reloaded = Workflow.objects.with_shape().get(pk=workflow.pk)
    occupancy = record_counts_on_states([state.pk for state in reloaded.states.all()])
    return reloaded.to_shape(occupancy=occupancy)


def authoring_record(  # noqa: PLR0913 - a trail entry about a graph has this many parts.
    *,
    workflow_code: str,
    verb: str,
    actor: str,
    now: datetime,
    correlation_id: UUID,
    before: str = "",
    after: str = "",
    metadata: dict[str, JsonValue] | None = None,
) -> ActivityCommand:
    """Build one trail entry about the shape of a lifecycle.

    ``origin`` stays ``SYSTEM``: the authoring routes collect no justification, and ``MANUAL``
    *demands* a reason — the trail would either lie about having one or the write would fail on an
    empty string. What makes the change attributable is ``actor``, which the JWT proved.

    Args:
        workflow_code: ``Workflow.code``; the trail addresses every graph change by its graph.
        verb: One of the authoring verbs of ``ActivityRecord.Verb``.
        actor: ``accounts.User.code`` of the ops lead making the change.
        now: Domain time, supplied by the caller.
        correlation_id: Threaded from the API boundary; every entry one request writes shares it, so
            "retiring this state withdrew those three arrows" reads back as one decision.
        before: Previous value as text, empty when the fact has no before.
        after: New value as text.
        metadata: Which node or edge was touched, and what moved.

    Returns:
        The command, ready for ``write_activity``.
    """
    return ActivityCommand(
        entity_type=WORKFLOW_ENTITY,
        entity_id=workflow_code,
        verb=verb,
        actor=actor,
        from_value=before,
        to_value=after,
        metadata=metadata or {},
        occurred_at=now,
        correlation_id=correlation_id,
    )
