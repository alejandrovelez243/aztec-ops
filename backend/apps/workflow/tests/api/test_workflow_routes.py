"""HTTP behaviour of ``GET /api/v1/workflows`` — the column set a Jira-style board draws from.

``TestCase``: the route reads and writes nothing, so there is no ``on_commit`` for a
never-committed transaction to hide.

The graphs are built here in Python rather than loaded from the seed fixtures, for the same reason
``portfolio.tests.scenario`` builds its own: the fixtures are the operation's real data and an
assertion about *which* states come back would fail the day somebody inserts a state, for a reason
that has nothing to do with this route. What is built here is the smallest pair of graphs that can
prove the four things the board depends on — every graph is listed, the states are in the
operator's order, a graph with no states is an empty list, and none of it is readable without a
credential.
"""

from typing import Any

from django.test import TestCase

from apps.accounts.tests.support import bearer, make_member
from apps.catalog.models import EngagementType
from apps.workflow.models import (
    AppliesTo,
    StateCategory,
    Workflow,
    WorkflowBinding,
    WorkflowState,
    WorkflowTransition,
)

WORKFLOWS_URL = "/api/v1/workflows"

#: The account every authenticated request here signs in as. Any member may read the shapes.
MEMBER_CODE = "camila.torres"

#: ``order`` values the states are *created* with, deliberately not the order they are created in.
#: An assertion that passed under insertion order would prove nothing about the operator's arrangement.
BLOCKED_ORDER = 3
IN_PROGRESS_ORDER = 2
BACKLOG_ORDER = 1


def _build_project_graph() -> Workflow:
    """Create the project graph the board tests read: three columns, arranged out of insertion order.

    The nodes are inserted last-column-first on purpose, so ``Meta.ordering`` is what puts them
    back in the operator's sequence and not the primary key.

    Returns:
        The saved project workflow, bound to one engagement type and holding three states.
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
    WorkflowState.objects.create(
        workflow=workflow,
        code="descubrimiento",
        label="Descubrimiento",
        category=StateCategory.BACKLOG,
        is_initial=True,
        order=BACKLOG_ORDER,
    )
    # One edge, so the assertion that no edge is published is made against a graph that has one.
    WorkflowTransition.objects.create(
        workflow=workflow,
        from_state=in_progress,
        to_state=blocked,
        label="Marcar como bloqueado",
        requires_reason=True,
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

    def test_no_transition_is_published_anywhere_in_the_document(self) -> None:
        """The invariant of this route: it publishes shape, never legality.

        The project graph owns an edge, so this fails loudly the day somebody adds edges here to
        save the board a request — which is what would let a client decide a move is legal without
        asking the project it is moving.
        """
        payload = self._get().content.decode()

        self.assertNotIn("Marcar como bloqueado", payload)
        self.assertNotIn("transitions", payload)
        self.assertNotIn("requires_reason", payload)


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
