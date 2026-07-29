"""HTTP behaviour of ``GET /api/v1/workflows`` — the graph a board and a lifecycle diagram draw from.

``TestCase``: the route reads and writes nothing, so there is no ``on_commit`` for a
never-committed transaction to hide.

The graphs are built here in Python rather than loaded from the seed fixtures, for the same reason
``portfolio.tests.scenario`` builds its own: the fixtures are the operation's real data and an
assertion about *which* states come back would fail the day somebody inserts a state, for a reason
that has nothing to do with this route. What is built here is the smallest pair of graphs that can
prove what the board depends on — every graph is listed, the states are in the operator's order, the
configured edges are published with their reason flag, a withdrawn edge is not, a graph with no
states or no edges answers with empty lists, and none of it is readable without a credential.

The load-bearing test in this module is
:class:`ConfiguredEdgeIsNotLegalityTestCase`: it holds the line that publishing the graph did not
move enforcement. It asserts both halves at once — the edge *is* in the document, and the record is
*still* refused — because either half alone is satisfiable by a mistake.
"""

from datetime import UTC, datetime
from typing import Any

from django.test import SimpleTestCase, TestCase

from apps.accounts.tests.support import bearer, make_member
from apps.catalog.models import EngagementType
from apps.workflow.domain.errors import (
    GuardRejected,
    RequiredFieldMissing,
    TransitionNotAllowed,
)
from apps.workflow.domain.views import WorkflowEdgeView, WorkflowShapeView
from apps.workflow.models import (
    AppliesTo,
    StateCategory,
    Workflow,
    WorkflowBinding,
    WorkflowState,
    WorkflowTransition,
)
from apps.workflow.services.transition import validate_transition

WORKFLOWS_URL = "/api/v1/workflows"

#: The account every authenticated request here signs in as. Any member may read the shapes.
MEMBER_CODE = "camila.torres"

#: ``order`` values the states are *created* with, deliberately not the order they are created in.
#: An assertion that passed under insertion order would prove nothing about the operator's arrangement.
BLOCKED_ORDER = 3
IN_PROGRESS_ORDER = 2
BACKLOG_ORDER = 1

#: Domain time for the enforcement calls. Fixed, because a guard that read the clock would make the
#: same test pass or fail depending on when it ran.
CHECKED_AT = datetime(2026, 1, 15, 9, 0, tzinfo=UTC)

#: The edge :func:`_build_project_graph` configures and then deactivates — the operator withdrew the
#: move. It exists as a row and must not exist as an arrow.
WITHDRAWN_EDGE = ("bloqueado", "ejecucion")


class _MovingRecord:
    """The little :func:`validate_transition` asks of an aggregate, and nothing else.

    A stand-in rather than a ``Project`` or a ``Task``, because the enforcement path takes a
    structural ``TransitionableEntity`` and this module must not learn what a project is to prove a
    rule that belongs to the workflow context. ``next_step`` is here because it is the attribute the
    seeded graphs actually name in ``requires_fields``.
    """

    def __init__(self, *, code: str, state: WorkflowState, next_step: str = "") -> None:
        self.code = code
        self.workflow_state = state
        self.workflow_state_id = state.pk
        self.next_step = next_step


def _build_project_graph() -> Workflow:
    """Create the project graph the board tests read: three columns, arranged out of insertion order.

    The nodes are inserted last-column-first on purpose, so ``Meta.ordering`` is what puts them
    back in the operator's sequence and not the primary key.

    The edges are the four cases the document has to distinguish: a plain move that names a required
    field, a move that demands a written reason *and* names a guard, a withdrawn move, and — by its
    absence — the pair no operator declared, since ``descubrimiento -> bloqueado`` is what a client
    inventing legality from the column list would assume exists.

    Returns:
        The saved project workflow, bound to one engagement type, holding three states and four
        edges of which one is inactive.
    """
    workflow = Workflow.objects.create(
        code="test-project-flow",
        name="Test project flow",
        applies_to=AppliesTo.PROJECT,
        is_default=True,
    )
    blocked = WorkflowState.objects.create(
        workflow=workflow,
        code="bloqueado",
        label="Bloqueado",
        category=StateCategory.BLOCKED,
        order=BLOCKED_ORDER,
        color="#dc2626",
    )
    in_progress = WorkflowState.objects.create(
        workflow=workflow,
        code="ejecucion",
        label="Ejecucion",
        category=StateCategory.IN_PROGRESS,
        order=IN_PROGRESS_ORDER,
        color="#2563eb",
    )
    backlog = WorkflowState.objects.create(
        workflow=workflow,
        code="descubrimiento",
        label="Descubrimiento",
        category=StateCategory.BACKLOG,
        is_initial=True,
        order=BACKLOG_ORDER,
    )
    WorkflowTransition.objects.create(
        workflow=workflow,
        from_state=backlog,
        to_state=in_progress,
        label="Iniciar ejecucion",
        requires_fields=["next_step"],
        order=1,
    )
    # Guarded on purpose: the document must publish this edge and must not publish its guard.
    WorkflowTransition.objects.create(
        workflow=workflow,
        from_state=in_progress,
        to_state=blocked,
        label="Marcar como bloqueado",
        requires_reason=True,
        guard="require_open_blocker",
        order=1,
    )
    WorkflowTransition.objects.create(
        workflow=workflow,
        from_state=in_progress,
        to_state=backlog,
        label="Devolver a descubrimiento",
        requires_reason=True,
        order=2,
    )
    # Withdrawn by the operator: configured once, not part of the graph today.
    WorkflowTransition.objects.create(
        workflow=workflow,
        from_state=blocked,
        to_state=in_progress,
        label="Reanudar",
        requires_reason=True,
        is_active=False,
        order=1,
    )
    WorkflowBinding.objects.create(
        workflow=workflow,
        applies_to=AppliesTo.PROJECT,
        engagement_type=EngagementType.objects.create(code="diagnostico", label="Diagnostico"),
    )
    return workflow


class WorkflowShapeDocumentTestCase(TestCase):
    """The document lists every configured graph, with what a board needs to draw its columns."""

    auth: dict[str, str]

    @classmethod
    def setUpTestData(cls) -> None:
        make_member(code=MEMBER_CODE)
        cls.auth = bearer(username=MEMBER_CODE)
        _build_project_graph()
        Workflow.objects.create(
            code="test-task-flow",
            name="Test task flow",
            applies_to=AppliesTo.TASK,
            is_default=True,
        )

    def _get(self) -> Any:
        return self.client.get(WORKFLOWS_URL, **self.auth)

    def test_every_configured_workflow_is_listed(self) -> None:
        body = self._get().json()

        self.assertEqual(
            {workflow["code"] for workflow in body["workflows"]},
            {"test-project-flow", "test-task-flow"},
        )

    def test_each_workflow_names_the_kind_of_aggregate_it_governs(self) -> None:
        by_code = {w["code"]: w for w in self._get().json()["workflows"]}

        self.assertEqual(by_code["test-project-flow"]["applies_to"], AppliesTo.PROJECT)
        self.assertEqual(by_code["test-task-flow"]["applies_to"], AppliesTo.TASK)

    def test_each_state_carries_code_label_category_and_colour(self) -> None:
        by_code = {w["code"]: w for w in self._get().json()["workflows"]}
        blocked = next(
            state
            for state in by_code["test-project-flow"]["states"]
            if state["code"] == "bloqueado"
        )

        self.assertEqual(
            blocked,
            {
                "code": "bloqueado",
                "label": "Bloqueado",
                "category": StateCategory.BLOCKED,
                "color": "#dc2626",
                # Published so an editor can say why a node may not be retired.
                # Nothing sits on this state in the fixture, hence zero.
                "record_count": 0,
                # Nothing stands on it, so retiring it would strand no record.
                "can_retire": True,
                "is_active": True,
                "order": 3,
                "is_initial": False,
                "is_terminal": False,
            },
        )

    def test_a_state_with_no_colour_reports_null_rather_than_an_empty_string(self) -> None:
        """Absence has to be visible, so the board falls back to its own neutral token."""
        by_code = {w["code"]: w for w in self._get().json()["workflows"]}
        backlog = next(
            state
            for state in by_code["test-project-flow"]["states"]
            if state["code"] == "descubrimiento"
        )

        self.assertIsNone(backlog["color"])

    def test_a_workflow_reports_the_engagement_types_bound_to_it(self) -> None:
        by_code = {w["code"]: w for w in self._get().json()["workflows"]}

        self.assertEqual(
            by_code["test-project-flow"]["engagement_types"],
            [{"code": "diagnostico", "label": "Diagnostico", "color": None}],
        )

    def test_a_workflow_bound_to_no_engagement_type_reports_an_empty_list(self) -> None:
        """The common case: the graph is reached as the per-kind default, named by nobody."""
        by_code = {w["code"]: w for w in self._get().json()["workflows"]}

        self.assertEqual(by_code["test-task-flow"]["engagement_types"], [])


class WorkflowGraphEdgeTestCase(TestCase):
    """The document publishes the graph an operator configured: every edge, with its reason flag."""

    auth: dict[str, str]

    @classmethod
    def setUpTestData(cls) -> None:
        make_member(code=MEMBER_CODE)
        cls.auth = bearer(username=MEMBER_CODE)
        _build_project_graph()

    def _graph(self) -> Any:
        body = self.client.get(WORKFLOWS_URL, **self.auth).json()
        return next(w for w in body["workflows"] if w["code"] == "test-project-flow")

    def test_every_configured_edge_is_published_with_both_of_its_endpoints(self) -> None:
        pairs = {(edge["from_state"], edge["to_state"]) for edge in self._graph()["transitions"]}

        self.assertEqual(
            pairs,
            {
                ("descubrimiento", "ejecucion"),
                ("ejecucion", "bloqueado"),
                ("ejecucion", "descubrimiento"),
            },
        )

    def test_an_edge_carries_the_operators_label_and_its_reason_flag(self) -> None:
        by_pair = {(e["from_state"], e["to_state"]): e for e in self._graph()["transitions"]}

        self.assertEqual(
            by_pair[("ejecucion", "bloqueado")],
            {
                "from_state": "ejecucion",
                "to_state": "bloqueado",
                "label": "Marcar como bloqueado",
                "requires_reason": True,
                "requires_fields": [],
            },
        )

    def test_an_edge_that_needs_no_reason_says_so_rather_than_omitting_the_flag(self) -> None:
        """A diagram marks the arrows that will ask for text, so the false has to be on the wire."""
        by_pair = {(e["from_state"], e["to_state"]): e for e in self._graph()["transitions"]}

        self.assertFalse(by_pair[("descubrimiento", "ejecucion")]["requires_reason"])

    def test_an_edge_names_the_aggregate_fields_the_operator_declared_mandatory(self) -> None:
        by_pair = {(e["from_state"], e["to_state"]): e for e in self._graph()["transitions"]}

        self.assertEqual(by_pair[("descubrimiento", "ejecucion")]["requires_fields"], ["next_step"])

    def test_no_edge_publishes_the_guard_that_may_still_refuse_it(self) -> None:
        """Naming the guard would invite the client to predict an answer it cannot compute."""
        payload = self.client.get(WORKFLOWS_URL, **self.auth).content.decode()

        self.assertNotIn("require_open_blocker", payload)
        self.assertNotIn("guard", payload)

    def test_edges_are_grouped_by_source_column_in_the_operators_order(self) -> None:
        """Arrows read in the same sequence as the columns, not in the order the states were saved.

        The nodes are inserted last-column-first by the builder, so an edge list ordered by the
        foreign key would start at ``ejecucion``.
        """
        self.assertEqual(
            [(e["from_state"], e["label"]) for e in self._graph()["transitions"]],
            [
                ("descubrimiento", "Iniciar ejecucion"),
                ("ejecucion", "Marcar como bloqueado"),
                ("ejecucion", "Devolver a descubrimiento"),
            ],
        )

    def test_an_edge_references_a_state_the_same_document_publishes_in_full(self) -> None:
        """Endpoints are codes because the node list is right there; a diagram resolves them."""
        graph = self._graph()
        published = {state["code"] for state in graph["states"]}

        endpoints = {e["from_state"] for e in graph["transitions"]} | {
            e["to_state"] for e in graph["transitions"]
        }
        self.assertTrue(endpoints <= published)


class WithdrawnEdgeTestCase(TestCase):
    """An edge an operator deactivated is absent from the graph, not published and flagged."""

    auth: dict[str, str]

    @classmethod
    def setUpTestData(cls) -> None:
        make_member(code=MEMBER_CODE)
        cls.auth = bearer(username=MEMBER_CODE)
        _build_project_graph()

    def _pairs(self) -> set[tuple[str, str]]:
        """The ``(from_state, to_state)`` pairs the document draws for the project graph."""
        body = self.client.get(WORKFLOWS_URL, **self.auth).json()
        graph = next(w for w in body["workflows"] if w["code"] == "test-project-flow")
        return {(edge["from_state"], edge["to_state"]) for edge in graph["transitions"]}

    def test_an_inactive_edge_is_not_drawn(self) -> None:
        """``is_active = False`` withdraws a move; drawing it would advertise a dead arrow."""
        self.assertNotIn(WITHDRAWN_EDGE, self._pairs())

    def test_reactivating_an_edge_in_the_admin_puts_the_arrow_back(self) -> None:
        """The graph is data: restoring a move is an ``is_active`` edit and zero code changes."""
        WorkflowTransition.objects.filter(label="Reanudar").update(is_active=True)

        self.assertIn(WITHDRAWN_EDGE, self._pairs())

    def test_the_projection_excludes_it_even_when_nobody_narrowed_the_query(self) -> None:
        """``to_shape`` re-checks the flag, so a caller that skipped the prefetch cannot leak it.

        The guarantee has to hold on the model, not only on the route: a withdrawn move surfacing
        because some other caller built the shape without chaining ``with_shape`` would be the same
        defect arriving through a different door.
        """
        workflow = Workflow.objects.get(code="test-project-flow")

        labels = [edge.label for edge in workflow.to_shape(occupancy={}).transitions]

        self.assertNotIn("Reanudar", labels)


class WorkflowStateOrderTestCase(TestCase):
    """States come back in the order an operator arranged, not alphabetical and not by insertion."""

    auth: dict[str, str]

    @classmethod
    def setUpTestData(cls) -> None:
        make_member(code=MEMBER_CODE)
        cls.auth = bearer(username=MEMBER_CODE)
        _build_project_graph()

    def test_states_are_returned_in_the_operators_configured_order(self) -> None:
        body = self.client.get(WORKFLOWS_URL, **self.auth).json()
        workflow = next(w for w in body["workflows"] if w["code"] == "test-project-flow")

        self.assertEqual(
            [state["code"] for state in workflow["states"]],
            ["descubrimiento", "ejecucion", "bloqueado"],
        )

    def test_reordering_a_state_in_the_admin_reorders_the_board(self) -> None:
        """The order is data: moving a column is an ``order`` edit and zero frontend changes."""
        WorkflowState.objects.filter(code="bloqueado").update(order=0)

        body = self.client.get(WORKFLOWS_URL, **self.auth).json()
        workflow = next(w for w in body["workflows"] if w["code"] == "test-project-flow")

        self.assertEqual(
            [state["code"] for state in workflow["states"]],
            ["bloqueado", "descubrimiento", "ejecucion"],
        )


class WorkflowWithoutStatesTestCase(TestCase):
    """A graph nobody has configured states for is an empty list, never an error."""

    auth: dict[str, str]

    @classmethod
    def setUpTestData(cls) -> None:
        make_member(code=MEMBER_CODE)
        cls.auth = bearer(username=MEMBER_CODE)
        Workflow.objects.create(
            code="test-empty-flow",
            name="Test empty flow",
            applies_to=AppliesTo.PROJECT,
        )

    def test_a_workflow_with_no_states_is_served_with_an_empty_state_list(self) -> None:
        response = self.client.get(WORKFLOWS_URL, **self.auth)

        self.assertEqual(response.status_code, 200)
        workflow = next(w for w in response.json()["workflows"] if w["code"] == "test-empty-flow")
        self.assertEqual(workflow["states"], [])


class WorkflowWithoutEdgesTestCase(TestCase):
    """A graph with columns and no declared move keeps its shape: an empty list, never an error."""

    auth: dict[str, str]

    @classmethod
    def setUpTestData(cls) -> None:
        make_member(code=MEMBER_CODE)
        cls.auth = bearer(username=MEMBER_CODE)
        workflow = Workflow.objects.create(
            code="test-edgeless-flow",
            name="Test edgeless flow",
            applies_to=AppliesTo.PROJECT,
        )
        WorkflowState.objects.create(
            workflow=workflow,
            code="unico",
            label="Unico",
            category=StateCategory.BACKLOG,
            is_initial=True,
        )

    def _graph(self) -> Any:
        body = self.client.get(WORKFLOWS_URL, **self.auth).json()
        return next(w for w in body["workflows"] if w["code"] == "test-edgeless-flow")

    def test_a_workflow_with_no_edges_is_served_with_an_empty_transition_list(self) -> None:
        """Isolated columns are a real configuration — an operator mid-setup — not a failure."""
        response = self.client.get(WORKFLOWS_URL, **self.auth)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(self._graph()["transitions"], [])

    def test_the_key_is_present_rather_than_omitted_so_the_client_shape_never_varies(self) -> None:
        """A generated client destructures ``transitions``; an absent key is a different type."""
        self.assertIn("transitions", self._graph())

    def test_a_workflow_with_no_edges_still_publishes_its_columns(self) -> None:
        self.assertEqual([state["code"] for state in self._graph()["states"]], ["unico"])


class WorkflowShapeDefaultsTestCase(SimpleTestCase):
    """The projection's own defaults, proved without a database so the purity of ``domain/`` holds."""

    def test_a_shape_built_without_edges_defaults_to_an_empty_tuple(self) -> None:
        """Nothing may construct a shape whose ``transitions`` is ``None``."""
        shape = WorkflowShapeView(code="w", name="W", applies_to=AppliesTo.PROJECT)

        self.assertEqual(shape.transitions, ())

    def test_an_edge_defaults_to_requiring_neither_a_reason_nor_a_field(self) -> None:
        edge = WorkflowEdgeView(from_state="a", to_state="b", label="Ir")

        self.assertFalse(edge.requires_reason)
        self.assertEqual(edge.requires_fields, ())


class ConfiguredEdgeIsNotLegalityTestCase(TestCase):
    """The invariant this route was reopened under: an edge is configuration, never a permission.

    Replaces the older assertion that no edge appeared anywhere in the document. That test defended
    a real rule with a proxy that has now stopped tracking it — the rule was never "the shape hides
    the graph", it was "a client cannot decide a move from the shape" — so each case here asserts
    both halves together: the edge **is** published, and the record is **still** refused by
    :func:`~apps.workflow.services.transition.validate_transition`. Asserting only the second half
    would pass on a document that publishes nothing, which is exactly the regression the original
    test was written to catch.
    """

    auth: dict[str, str]

    @classmethod
    def setUpTestData(cls) -> None:
        make_member(code=MEMBER_CODE)
        cls.auth = bearer(username=MEMBER_CODE)
        _build_project_graph()

    def _published_edges(self) -> set[tuple[str, str]]:
        """The ``(from_state, to_state)`` pairs the workflow document draws for the project graph."""
        body = self.client.get(WORKFLOWS_URL, **self.auth).json()
        graph = next(w for w in body["workflows"] if w["code"] == "test-project-flow")
        return {(edge["from_state"], edge["to_state"]) for edge in graph["transitions"]}

    def _record_on(self, state_code: str, *, next_step: str = "") -> _MovingRecord:
        state = WorkflowState.objects.get(workflow__code="test-project-flow", code=state_code)
        return _MovingRecord(code="PRJ-TEST", state=state, next_step=next_step)

    def test_an_edge_the_record_is_not_standing_on_is_published_and_still_refused(self) -> None:
        """The graph says the move exists somewhere; the record says not from here."""
        self.assertIn(("ejecucion", "bloqueado"), self._published_edges())

        with self.assertRaises(TransitionNotAllowed):
            validate_transition(
                entity=self._record_on("descubrimiento"),
                to_state_code="bloqueado",
                actor=MEMBER_CODE,
                now=CHECKED_AT,
                reason="El cliente no responde",
            )

    def test_a_guarded_edge_is_published_and_the_guard_still_refuses_the_record(self) -> None:
        """``GuardRejected`` is why an edge existing cannot mean a move is legal.

        The record is on the edge's source state and supplies the reason the edge demands, so
        everything the document could possibly tell a client is satisfied — and the move is refused
        anyway, on a fact about the record that no graph holds.
        """
        self.assertIn(("ejecucion", "bloqueado"), self._published_edges())

        with self.assertRaises(GuardRejected):
            validate_transition(
                entity=self._record_on("ejecucion"),
                to_state_code="bloqueado",
                actor=MEMBER_CODE,
                now=CHECKED_AT,
                reason="Esperando accesos del cliente",
                guard_attributes={"open_blocker_count": 0},
            )

    def test_a_required_field_is_published_as_a_requirement_and_checked_on_the_record(self) -> None:
        """``requires_fields`` names an attribute of the aggregate, so only the row answers it."""
        self.assertIn(("descubrimiento", "ejecucion"), self._published_edges())

        with self.assertRaises(RequiredFieldMissing):
            validate_transition(
                entity=self._record_on("descubrimiento", next_step=""),
                to_state_code="ejecucion",
                actor=MEMBER_CODE,
                now=CHECKED_AT,
            )

    def test_the_same_edge_is_taken_once_the_record_satisfies_it(self) -> None:
        """The refusals above are about the record, not about the graph being unusable."""
        check = validate_transition(
            entity=self._record_on("descubrimiento", next_step="Agendar kickoff"),
            to_state_code="ejecucion",
            actor=MEMBER_CODE,
            now=CHECKED_AT,
        )

        self.assertEqual(check.to_state_code, "ejecucion")
        self.assertEqual(check.checked_fields, ("next_step",))

    def test_the_enforcement_path_reads_the_rows_and_not_the_published_document(self) -> None:
        """Deactivating an edge refuses the move, whatever any client cached from the graph."""
        WorkflowTransition.objects.filter(label="Iniciar ejecucion").update(is_active=False)

        with self.assertRaises(TransitionNotAllowed):
            validate_transition(
                entity=self._record_on("descubrimiento", next_step="Agendar kickoff"),
                to_state_code="ejecucion",
                actor=MEMBER_CODE,
                now=CHECKED_AT,
            )


class WorkflowRouteAuthenticationTestCase(TestCase):
    """Authenticated like every other read: the route is not on the closed opt-out list."""

    @classmethod
    def setUpTestData(cls) -> None:
        make_member(code=MEMBER_CODE)
        _build_project_graph()

    def test_reading_the_shapes_without_a_credential_is_refused(self) -> None:
        response = self.client.get(WORKFLOWS_URL)

        self.assertEqual(response.status_code, 401)

    def test_reading_the_shapes_with_a_credential_succeeds(self) -> None:
        response = self.client.get(WORKFLOWS_URL, **bearer(username=MEMBER_CODE))

        self.assertEqual(response.status_code, 200)


class RetiredWorkflowTestCase(TestCase):
    """A retired graph is still published, flagged, because aggregates still sit on its states."""

    auth: dict[str, str]

    @classmethod
    def setUpTestData(cls) -> None:
        make_member(code=MEMBER_CODE)
        cls.auth = bearer(username=MEMBER_CODE)
        workflow = _build_project_graph()
        Workflow.objects.filter(pk=workflow.pk).update(is_active=False)

    def test_a_retired_workflow_is_served_with_its_columns_and_is_active_false(self) -> None:
        body = self.client.get(WORKFLOWS_URL, **self.auth).json()
        workflow = next(w for w in body["workflows"] if w["code"] == "test-project-flow")

        self.assertFalse(workflow["is_active"])
        self.assertEqual(len(workflow["states"]), 3)
