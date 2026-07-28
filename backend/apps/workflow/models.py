"""Persistence for the configurable state machines (DATA_MODEL §2).

The state graph is data, not code: a `Workflow` owns `WorkflowState` nodes and
`WorkflowTransition` edges, and a `WorkflowBinding` decides which graph an engagement type
follows. Adding a state or an edge is a row, never a deploy.

This module holds fields, constraints, indexes and `clean()` validations only. The rules that
decide whether a move is legal live in `services/transition.py`; every query lives in
`repositories.py`.
"""

from typing import ClassVar

from django.core.exceptions import ValidationError
from django.db import models


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


class Workflow(models.Model):
    """A named state graph bound to one entity kind.

    At most one workflow per `applies_to` is the default, and that row is the last resort of the
    binding resolution in `repositories.resolve_workflow`. Retirement is `is_active = False`, never
    a delete: states referenced by live projects are `PROTECT`ed on purpose.
    """

    code = models.CharField(max_length=32)
    name = models.CharField(max_length=96)
    applies_to = models.CharField(max_length=8, choices=AppliesTo)
    is_default = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

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


class WorkflowBinding(models.Model):
    """Which workflow an engagement type follows, for one entity kind.

    Resolution order, implemented in `repositories.resolve_workflow`: the binding for the
    aggregate's engagement type, then the binding whose `engagement_type` is null, then
    `Workflow.is_default`. A Diagnostico can therefore run a shorter lifecycle than a recurring
    maintenance engagement with no code change.

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
