"""Which lifecycle a record follows: the precedence ladder, one rung per test.

``Workflow.objects.resolve`` is the single statement of that order, and it is worth its own file
because every rung is reachable from the admin: an ops lead can bind an engagement type, move the
default, or put one record on a graph of its own, and the four decisions have to compose in exactly
one way. A test per rung is what keeps "the record wins" from quietly becoming "the binding wins"
the next time somebody reorders the body.

``TestCase``: these are database reads with no outbox and no ``on_commit`` behind them.
"""

from django.test import TestCase

from apps.catalog.models import EngagementType
from apps.workflow.domain.errors import WorkflowNotConfigured
from apps.workflow.models import (
    AppliesTo,
    StateCategory,
    Workflow,
    WorkflowBinding,
    WorkflowState,
)

#: The four graphs, one per rung of the ladder, named after the rung they are meant to answer from.
RECORD_GRAPH = "ciclo-del-registro"
TYPE_GRAPH = "ciclo-por-tipo"
KIND_GRAPH = "ciclo-por-defecto"
FALLBACK_GRAPH = "ciclo-fallback"


def _graph(code: str, *, is_default: bool = False) -> Workflow:
    """Create one project graph with a single entry node, which is all resolution needs."""
    workflow = Workflow.objects.create(
        code=code,
        name=code.replace("-", " ").capitalize(),
        applies_to=AppliesTo.PROJECT,
        is_default=is_default,
    )
    WorkflowState.objects.create(
        workflow=workflow,
        code="inicio",
        label="Inicio",
        category=StateCategory.BACKLOG,
        is_initial=True,
        order=0,
    )
    return workflow


class WorkflowResolutionPrecedenceTestCase(TestCase):
    """The four rungs, most specific first, each proved to outrank the one below it."""

    @classmethod
    def setUpTestData(cls) -> None:
        cls.record_graph = _graph(RECORD_GRAPH)
        cls.type_graph = _graph(TYPE_GRAPH)
        cls.kind_graph = _graph(KIND_GRAPH)
        cls.fallback_graph = _graph(FALLBACK_GRAPH, is_default=True)

        cls.bound_type = EngagementType.objects.create(code="diagnostico", label="Diagnóstico")
        cls.unbound_type = EngagementType.objects.create(code="proyecto", label="Proyecto")

        cls.type_binding = WorkflowBinding.objects.create(
            workflow=cls.type_graph,
            applies_to=AppliesTo.PROJECT,
            engagement_type=cls.bound_type,
        )
        cls.kind_binding = WorkflowBinding.objects.create(
            workflow=cls.kind_graph,
            applies_to=AppliesTo.PROJECT,
            engagement_type=None,
        )

    def test_the_graph_a_record_names_outranks_the_binding_of_its_engagement_type(self) -> None:
        resolved = Workflow.objects.resolve(
            applies_to=AppliesTo.PROJECT,
            engagement_type_id=self.bound_type.pk,
            assigned=self.record_graph,
        )

        self.assertEqual(resolved.code, RECORD_GRAPH)

    def test_a_record_keeps_its_own_graph_even_after_that_graph_is_retired(self) -> None:
        # Retirement stops a lifecycle being *offered* to new work; a record already inside one is
        # not new work, and the states it stands on are PROTECTed exactly so.
        Workflow.objects.filter(pk=self.record_graph.pk).update(is_active=False)
        retired = Workflow.objects.get(pk=self.record_graph.pk)

        resolved = Workflow.objects.resolve(
            applies_to=AppliesTo.PROJECT,
            engagement_type_id=self.bound_type.pk,
            assigned=retired,
        )

        self.assertEqual(resolved.code, RECORD_GRAPH)

    def test_the_engagement_type_binding_answers_a_record_that_names_no_graph(self) -> None:
        resolved = Workflow.objects.resolve(
            applies_to=AppliesTo.PROJECT, engagement_type_id=self.bound_type.pk
        )

        self.assertEqual(resolved.code, TYPE_GRAPH)

    def test_the_per_kind_default_binding_answers_an_engagement_type_nobody_bound(self) -> None:
        resolved = Workflow.objects.resolve(
            applies_to=AppliesTo.PROJECT, engagement_type_id=self.unbound_type.pk
        )

        self.assertEqual(resolved.code, KIND_GRAPH)

    def test_is_default_answers_last_when_no_binding_matches_at_all(self) -> None:
        WorkflowBinding.objects.filter(pk__in=[self.type_binding.pk, self.kind_binding.pk]).update(
            is_active=False
        )

        resolved = Workflow.objects.resolve(
            applies_to=AppliesTo.PROJECT, engagement_type_id=self.bound_type.pk
        )

        self.assertEqual(resolved.code, FALLBACK_GRAPH)

    def test_nothing_answering_for_a_kind_is_loud_rather_than_a_guess(self) -> None:
        with self.assertRaises(WorkflowNotConfigured):
            Workflow.objects.resolve(applies_to=AppliesTo.TASK)


class WorkflowResolutionRegressionTestCase(TestCase):
    """The behaviour that existed before records could name a graph, unchanged.

    The nullable column is the whole risk of this feature: every project and task in the database
    carries ``NULL`` in it, so the ladder has to answer for them exactly as it did when its first
    rung did not exist.
    """

    @classmethod
    def setUpTestData(cls) -> None:
        cls.type_graph = _graph(TYPE_GRAPH)
        cls.fallback_graph = _graph(FALLBACK_GRAPH, is_default=True)
        cls.bound_type = EngagementType.objects.create(code="diagnostico", label="Diagnóstico")
        WorkflowBinding.objects.create(
            workflow=cls.type_graph,
            applies_to=AppliesTo.PROJECT,
            engagement_type=cls.bound_type,
        )

    def test_an_unassigned_record_resolves_through_the_binding_exactly_as_before(self) -> None:
        self.assertEqual(
            Workflow.objects.resolve(
                applies_to=AppliesTo.PROJECT,
                engagement_type_id=self.bound_type.pk,
                assigned=None,
            ).code,
            TYPE_GRAPH,
        )

    def test_omitting_the_new_argument_altogether_is_the_same_call_it_always_was(self) -> None:
        self.assertEqual(
            Workflow.objects.resolve(
                applies_to=AppliesTo.PROJECT, engagement_type_id=self.bound_type.pk
            ).code,
            TYPE_GRAPH,
        )
