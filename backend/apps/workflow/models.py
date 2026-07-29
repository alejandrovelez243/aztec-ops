"""Persistence for the configurable state machines (DATA_MODEL §2).

The state graph is data, not code: a `Workflow` owns `WorkflowState` nodes and
`WorkflowTransition` edges, and a `WorkflowBinding` decides which graph an engagement type
follows. Adding a state or an edge is a row, never a deploy.

This module holds fields, constraints, indexes, `clean()` validations and the named queries each
model answers about itself (CLAUDE.md rule 6) — `WorkflowTransition.objects.active().from_state(...)`
is the legal-move set, `Workflow.objects.resolve(...)` is the binding resolution of §2. The rule
that decides whether a proposed move is legal stays in `services/transition.py`: this module can
say which edges exist, never whether a guard passes.
"""

from typing import ClassVar, Self

from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import F, Q

from apps.shared.refs import StateRef
from apps.workflow.domain.errors import WorkflowNotConfigured
from apps.workflow.domain.views import (
    TransitionOption,
    WorkflowShapeView,
    required_field_names,
)


class AppliesTo(models.TextChoices):
    """The kind of aggregate a workflow governs.

    Structural, not operator data: a third entity kind would be a code change everywhere, so it is
    a `TextChoices` plus a migration rather than a taxonomy row.
    """

    PROJECT = "PROJECT", "Project"
    TASK = "TASK", "Task"


class StateCategory(models.TextChoices):
    """The closed vocabulary every other context branches on (DATA_MODEL §12).

    This is the one legitimate `TextChoices` for a business concept in this codebase, and it is
    legitimate precisely because it is structural rather than operational. `WorkflowState.code` is
    open-ended — the operation invents `en_espera_cliente` from the admin and nothing in Python
    reacts. `category` is a fixed set of five values the operator picks from but cannot extend, so
    "is this blocked", "is this open", "is this closed" stay single predicates: the risk
    specifications, the priority signals and the snapshot counts read `category` and are correct on
    the next event, with no migration and no deploy.

    Branching on `code` instead is the failure this split exists to prevent: `code` is unique only
    inside its workflow, so two workflows may both own `bloqueada`, and any list of blocked codes
    written in Python is a grep that will eventually miss one place.
    """

    BACKLOG = "BACKLOG", "Backlog"
    IN_PROGRESS = "IN_PROGRESS", "In progress"
    BLOCKED = "BLOCKED", "Blocked"
    DONE = "DONE", "Done"
    CANCELLED = "CANCELLED", "Cancelled"


class WorkflowQuerySet(models.QuerySet["Workflow"]):
    """The named ways to narrow the set of state graphs."""

    def active(self) -> Self:
        """The graphs still in service; retirement is `is_active = False`, never a delete."""
        return self.filter(is_active=True)

    def for_kind(self, applies_to: str) -> Self:
        """The graphs governing one kind of aggregate — `AppliesTo.PROJECT` or `AppliesTo.TASK`."""
        return self.filter(applies_to=applies_to)

    def default(self) -> Self:
        """The fallback graph of its kind: at most one per `applies_to`, by unique constraint."""
        return self.filter(is_default=True)

    def with_shape(self) -> Self:
        """Load what :meth:`Workflow.to_shape` reads, in two extra queries for the whole chain.

        Prefetches the nodes and the engagement-type bindings of every selected graph, so the board
        document costs three index scans instead of two per workflow. The bindings are prefetched
        already narrowed to :meth:`WorkflowBindingQuerySet.engagement_typed`, which is why
        `to_shape` re-checks each one: the projection has to stay correct when nobody chained this.
        """
        return self.prefetch_related(
            "states",
            models.Prefetch("bindings", queryset=WorkflowBinding.objects.engagement_typed()),
        )


#: `from_queryset` builds the manager that carries every `WorkflowQuerySet` method; it is bound to
#: a name because a class cannot inherit from a call expression and stay typed, and subscripted
#: with the model so `self.active()` inside the manager keeps returning `Workflow` rows.
_WorkflowManagerBase = models.Manager.from_queryset(WorkflowQuerySet)


class WorkflowManager(_WorkflowManagerBase["Workflow"]):
    """Adds the one question that is not a filter over `Workflow` alone.

    `resolve` reads `WorkflowBinding` before falling back to this table, so it cannot be a queryset
    method without lying about what the chain selects — which is exactly the case
    `Manager.from_queryset` exists for.
    """

    def resolve(self, *, applies_to: str, engagement_type_id: int | None = None) -> "Workflow":
        """The workflow an aggregate of this kind and engagement type follows (DATA_MODEL §2).

        Resolution order, most specific first: the active binding for that engagement type, then
        the binding whose `engagement_type` is null (the per-kind default binding), then
        `is_default` on this table. That order is what lets a Diagnostico run a shorter lifecycle
        than a recurring maintenance engagement by inserting one row.

        Materialises: it answers with a single row and ends the chain.

        Args:
            applies_to: `AppliesTo` value — `PROJECT` or `TASK`.
            engagement_type_id: `catalog.EngagementType` primary key, or `None` to ask only for the
                per-kind default.

        Returns:
            The active workflow to bind the aggregate to.

        Raises:
            WorkflowNotConfigured: Neither a binding nor a default answers for this entity kind.
        """
        bindings = list(
            WorkflowBinding.objects.active()
            .for_kind(applies_to)
            .matching_engagement_type(engagement_type_id)
            .select_related("workflow")
        )
        specific = next((b for b in bindings if b.engagement_type_id is not None), None)
        if specific is not None:
            return specific.workflow

        per_kind_default = next((b for b in bindings if b.engagement_type_id is None), None)
        if per_kind_default is not None:
            return per_kind_default.workflow

        default = self.active().for_kind(applies_to).default().first()
        if default is None:
            raise WorkflowNotConfigured(applies_to)
        return default


class Workflow(models.Model):
    """A named state graph bound to one entity kind.

    At most one workflow per `applies_to` is the default, and that row is the last resort of
    `Workflow.objects.resolve`. Retirement is `is_active = False`, never a delete: states
    referenced by live projects are `PROTECT`ed on purpose.
    """

    code = models.CharField(max_length=32)
    name = models.CharField(max_length=96)
    applies_to = models.CharField(max_length=8, choices=AppliesTo)
    is_default = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    objects = WorkflowManager()

    class Meta:
        """DATA_MODEL §9.1: the slug is the contract, and one fallback per entity kind."""

        ordering: ClassVar[list[str]] = ["applies_to", "code"]
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.UniqueConstraint(fields=["code"], name="uniq_workflow_code"),
            models.UniqueConstraint(
                fields=["applies_to"],
                condition=models.Q(is_default=True),
                name="uniq_default_workflow_per_applies_to",
            ),
        ]

    def __str__(self) -> str:
        """Name and entity kind, which is what disambiguates two graphs in an admin picker."""
        return f"{self.name} ({self.applies_to})"

    def to_shape(self) -> WorkflowShapeView:
        """Describe this graph as the column set a board draws.

        Publishes every node — including the ones no aggregate currently occupies, which is the
        whole point — and no edge at all: which move is legal is answered per project by
        ``transitions`` on the detail, never by this shape.

        Reads ``states`` and ``bindings``, so callers chain
        :meth:`WorkflowQuerySet.with_shape`; without it this is two queries per workflow. Bindings
        are re-filtered here rather than trusted from the prefetch, so an unprefetched call
        describes the same graph instead of raising on the per-kind default binding, whose
        ``engagement_type`` is null by definition.

        Returns:
            The graph as an immutable
            :class:`~apps.workflow.domain.views.WorkflowShapeView`, its states in the operator's
            ``order`` and its engagement types in the catalog's.
        """
        return WorkflowShapeView(
            code=self.code,
            name=self.name,
            applies_to=self.applies_to,
            is_default=self.is_default,
            is_active=self.is_active,
            engagement_types=tuple(
                binding.engagement_type.to_ref()
                for binding in self.bindings.all()
                if binding.is_active and binding.engagement_type is not None
            ),
            states=tuple(state.to_ref() for state in self.states.all()),
        )


class WorkflowStateQuerySet(models.QuerySet["WorkflowState"]):
    """The named ways to select nodes of a graph."""

    def for_workflow(self, workflow_id: int) -> Self:
        """The nodes of one graph, in the operator's order (`Meta.ordering`)."""
        return self.filter(workflow_id=workflow_id)

    def initial(self) -> Self:
        """The entry nodes; a partial unique constraint allows at most one per workflow."""
        return self.filter(is_initial=True)

    def entry_state(self, *, workflow_id: int) -> "WorkflowState":
        """The node a brand new aggregate of this workflow starts on.

        Asked by every creation service, which is why it refuses to guess: a workflow with no entry
        node cannot place a new aggregate anywhere, and silently picking the lowest `order` would
        hide a broken graph behind a plausible state. Materialises: it ends the chain.

        Args:
            workflow_id: `Workflow` primary key.

        Returns:
            The single state flagged `is_initial`.

        Raises:
            WorkflowNotConfigured: The workflow has no entry node.
        """
        state = self.for_workflow(workflow_id).initial().first()
        if state is None:
            raise WorkflowNotConfigured(f"workflow:{workflow_id}")
        return state


class WorkflowState(models.Model):
    """A node in a workflow graph.

    `code` is identity and is unique inside its workflow only; `category` is the semantics every
    other context queries. Exactly one state per workflow carries `is_initial`, which is what a
    creation service uses to place a brand new aggregate without hardcoding a slug.
    """

    workflow = models.ForeignKey(Workflow, on_delete=models.PROTECT, related_name="states")
    code = models.CharField(max_length=32)
    label = models.CharField(max_length=64)
    category = models.CharField(max_length=16, choices=StateCategory)
    is_initial = models.BooleanField(default=False)
    is_terminal = models.BooleanField(default=False)
    order = models.SmallIntegerField(default=0)
    color = models.CharField(max_length=7, default="", blank=True)

    objects = WorkflowStateQuerySet.as_manager()

    class Meta:
        """DATA_MODEL §9.1 and §10: a code is unique inside its graph; one entry node."""

        ordering: ClassVar[list[str]] = ["workflow", "order", "code"]
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.UniqueConstraint(
                fields=["workflow", "code"], name="uniq_state_code_per_workflow"
            ),
            models.UniqueConstraint(
                fields=["workflow"],
                condition=models.Q(is_initial=True),
                name="uniq_initial_state_per_workflow",
            ),
        ]
        indexes: ClassVar[list[models.Index]] = [
            models.Index(fields=["workflow", "order"], name="idx_state_workflow_order"),
            models.Index(fields=["category"], name="idx_state_category"),
        ]

    def __str__(self) -> str:
        """Label plus category, because the category is what every other context branches on."""
        return f"{self.label} [{self.category}]"

    def to_ref(self) -> StateRef:
        """Describe this node as the reference shape every payload carrying a state renders.

        Issues no query: all four values are columns of this row.

        Returns:
            The state as an immutable :class:`~apps.shared.refs.StateRef`.
        """
        return StateRef.of(
            code=self.code,
            label=self.label,
            category=self.category,
            color=self.color,
        )


class WorkflowTransitionQuerySet(models.QuerySet["WorkflowTransition"]):
    """The legal-move set, sliced the three ways the operation asks for it.

    `active().from_state(id)` is the definition the whole context turns on: it is what the detail
    endpoint renders as buttons and what the transition service validates against, and the two must
    never disagree — which is why `is_active` is filtered here rather than by each caller.
    """

    def active(self) -> Self:
        """The edges an operator may take; an inactive edge is invisible, not merely hidden."""
        return self.filter(is_active=True)

    def from_state(self, state_id: int) -> Self:
        """The edges leaving a state, in the order the operator arranged them (`Meta.ordering`).

        Served by the `(from_state, is_active)` index.

        Args:
            state_id: `WorkflowState` primary key the aggregate currently sits on.
        """
        return self.filter(from_state_id=state_id)

    def to_state_code(self, code: str) -> Self:
        """The edges landing on a state code *inside the source state's own workflow*.

        `WorkflowState.code` is unique per workflow only, so two graphs may both own `bloqueada`;
        matching the target's workflow against the source's is what stops an identical code in
        another graph from being reached by accident.

        Args:
            code: `WorkflowState.code` of the target.
        """
        return self.filter(to_state__code=code, to_state__workflow=F("from_state__workflow"))

    def for_workflow(self, workflow_id: int) -> Self:
        """Every edge of one graph, read off the denormalized `workflow` column without a join."""
        return self.filter(workflow_id=workflow_id)

    def with_states(self) -> Self:
        """Load both endpoints and the workflow, for callers that read the labels off each edge."""
        return self.select_related("from_state", "to_state", "workflow")

    def as_options(self) -> tuple[TransitionOption, ...]:
        """Materialise the selected edges as the projection the detail endpoint renders.

        Ends the chain: the query executes here, so nothing downstream can add a predicate after
        the button list was decided. Chain :meth:`with_states` first — each option reads the
        target state's label, category and color, which is one query per edge otherwise.

        Returns:
            The edges as immutable :class:`~apps.workflow.domain.views.TransitionOption` values,
            in ``Meta.ordering`` — the order the operator arranged the buttons in.
        """
        return tuple(transition.to_option() for transition in self)

    def target_codes(self) -> tuple[str, ...]:
        """The ``to_state`` codes of the selected edges. Materialises: it ends the chain.

        This is what ``details.allowed`` carries on a 409 ``transition_not_allowed`` (`docs/API.md`
        §1.5), so a client whose button list went stale resyncs from the rejection instead of
        refetching the whole project.
        """
        return tuple(self.values_list("to_state__code", flat=True))


class WorkflowTransition(models.Model):
    """A legal edge between two states of the same workflow.

    An edge that does not exist, or exists with `is_active = False`, makes the move illegal: the
    transition service raises `TransitionNotAllowed` (HTTP 409) instead of assigning the state.
    Re-enabling an edge is `is_active = True`, never a second row for the same ordered pair — that
    is what the unique constraint enforces.

    `workflow` is denormalized from `from_state.workflow` so "every edge of this workflow" is one
    index scan; `clean()` is what keeps the copy honest, since the check spans three rows and
    cannot be a database `CHECK` (DATA_MODEL §9.3).
    """

    workflow = models.ForeignKey(Workflow, on_delete=models.PROTECT, related_name="transitions")
    from_state = models.ForeignKey(WorkflowState, on_delete=models.PROTECT, related_name="outgoing")
    to_state = models.ForeignKey(WorkflowState, on_delete=models.PROTECT, related_name="incoming")
    label = models.CharField(max_length=64)
    requires_reason = models.BooleanField(default=False)
    requires_fields = models.JSONField(default=list, blank=True)
    guard = models.CharField(max_length=64, default="", blank=True)
    is_active = models.BooleanField(default=True)
    order = models.SmallIntegerField(default=0)

    objects = WorkflowTransitionQuerySet.as_manager()

    class Meta:
        """One edge per ordered pair; re-enabling is `is_active`, never a second row."""

        ordering: ClassVar[list[str]] = ["from_state", "order", "id"]
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.UniqueConstraint(
                fields=["from_state", "to_state"], name="uniq_transition_state_pair"
            ),
        ]
        indexes: ClassVar[list[models.Index]] = [
            models.Index(fields=["from_state", "is_active"], name="idx_transition_from_active"),
        ]

    def __str__(self) -> str:
        """The edge as it reads on a button: source, target and the operator's label."""
        return f"{self.from_state.code} -> {self.to_state.code} ({self.label})"

    def clean(self) -> None:
        """Refuse an edge whose three workflow references disagree.

        The admin is the realistic entry point for a bad edge — the state pickers list every state
        in the database — so the mismatch is caught here with a readable message instead of
        surfacing later as an aggregate sitting in a state its own workflow does not contain.

        Raises:
            ValidationError: `from_state` or `to_state` belongs to another workflow.
        """
        # The stubs type a non-null FK's ``*_id`` as ``int`` because that is what a *saved* row
        # holds; on the unsaved instance ``clean()` actually runs against, an unfilled picker
        # leaves it ``None``. Binding through an optional local states that fact once.
        workflow_id: int | None = self.workflow_id
        from_state_id: int | None = self.from_state_id
        to_state_id: int | None = self.to_state_id
        if workflow_id is None or from_state_id is None or to_state_id is None:
            return
        if self.from_state.workflow_id != self.workflow_id:
            raise ValidationError({"from_state": "The source state belongs to another workflow."})
        if self.to_state.workflow_id != self.workflow_id:
            raise ValidationError({"to_state": "The target state belongs to another workflow."})

    def to_option(self) -> TransitionOption:
        """Describe this edge as the button the frontend renders.

        The projection carries what a client needs to *decide* before acting — the target state,
        the operator's label, whether a reason is mandatory and which aggregate fields must be
        filled first — and nothing that would let it act without asking: no primary key, no guard
        name, no workflow id. The guard is deliberately omitted; whether it passes depends on facts
        this row does not hold, so advertising it would invite the client to predict the answer.

        Reads ``to_state``, so callers chain :meth:`WorkflowTransitionQuerySet.with_states`.

        Returns:
            The edge as an immutable :class:`~apps.workflow.domain.views.TransitionOption`.
        """
        return TransitionOption(
            to_state=self.to_state.to_ref(),
            label=self.label,
            requires_reason=self.requires_reason,
            requires_fields=required_field_names(self.requires_fields),
        )


class WorkflowBindingQuerySet(models.QuerySet["WorkflowBinding"]):
    """The candidate set `Workflow.objects.resolve` picks the most specific row out of."""

    def active(self) -> Self:
        """Bindings that can be used today: the row *and* the graph it points at are in service."""
        return self.filter(is_active=True, workflow__is_active=True)

    def for_kind(self, applies_to: str) -> Self:
        """The bindings for one kind of aggregate, off the denormalized column — no join."""
        return self.filter(applies_to=applies_to)

    def matching_engagement_type(self, engagement_type_id: int | None) -> Self:
        """The binding for this engagement type together with the per-kind default binding.

        Both candidates in one query, because the fallback is decided in Python over at most two
        rows; a second round trip to discover the default would cost more than reading it.

        Args:
            engagement_type_id: `catalog.EngagementType` primary key, or `None` to ask for the
                per-kind default only — which the null-engagement-type clause already returns.
        """
        return self.filter(
            Q(engagement_type_id=engagement_type_id) | Q(engagement_type__isnull=True)
        )

    def engagement_typed(self) -> Self:
        """The active bindings that name a specific engagement type, ready to render.

        The per-kind default binding — the row whose `engagement_type` is null — is excluded on
        purpose: it attributes a graph to no engagement type, and publishing it as one would make
        a board believe the fallback is a specific choice.

        Ordered by the catalog's own `(order, code)` rather than by this table's `Meta.ordering`,
        which sorts by the foreign key and so would list the types in the order somebody inserted
        them. `select_related` loads the engagement type each caller reads the label off.

        Filters `is_active` on the binding row alone, unlike :meth:`active`, which additionally
        requires the graph to be in service because it answers "may this be resolved through
        today". Here the question is only which types a graph is named by; whether the graph itself
        is retired is a fact the caller reports separately, and folding it in would make a retired
        graph describe itself differently depending on whether the caller prefetched.
        """
        return (
            self.filter(is_active=True)
            .exclude(engagement_type__isnull=True)
            .select_related("engagement_type")
            .order_by("engagement_type__order", "engagement_type__code")
        )


class WorkflowBinding(models.Model):
    """Which workflow an engagement type follows, for one entity kind.

    Resolution order, implemented in `Workflow.objects.resolve`: the binding for the aggregate's
    engagement type, then the binding whose `engagement_type` is null, then `Workflow.is_default`.
    A Diagnostico can therefore run a shorter lifecycle than a recurring maintenance engagement
    with no code change.

    `applies_to` is denormalized from the workflow so the lookup does not join; `clean()` keeps the
    copy honest.
    """

    workflow = models.ForeignKey(Workflow, on_delete=models.PROTECT, related_name="bindings")
    applies_to = models.CharField(max_length=8, choices=AppliesTo)
    engagement_type = models.ForeignKey(
        "catalog.EngagementType",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="workflow_bindings",
    )
    is_active = models.BooleanField(default=True)

    objects = WorkflowBindingQuerySet.as_manager()

    class Meta:
        """One binding per engagement type per entity kind; the null row is the default."""

        ordering: ClassVar[list[str]] = ["applies_to", "engagement_type"]
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.UniqueConstraint(
                fields=["applies_to", "engagement_type"], name="uniq_binding_per_engagement_type"
            ),
        ]

    def __str__(self) -> str:
        """Reads as the resolution it encodes: kind/engagement type -> workflow."""
        target = self.engagement_type.code if self.engagement_type else "default"
        return f"{self.applies_to}/{target} -> {self.workflow.code}"

    def clean(self) -> None:
        """Refuse a binding that claims an entity kind its workflow does not govern.

        Raises:
            ValidationError: `applies_to` differs from the bound workflow's own `applies_to`.
        """
        # See ``WorkflowTransition.clean``: ``workflow_id`` is only non-null once the row is saved.
        workflow_id: int | None = self.workflow_id
        if workflow_id is None:
            return
        if self.applies_to != self.workflow.applies_to:
            raise ValidationError(
                {"applies_to": f"The bound workflow applies to {self.workflow.applies_to}."}
            )
