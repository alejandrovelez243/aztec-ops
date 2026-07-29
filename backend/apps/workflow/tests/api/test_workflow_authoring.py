"""HTTP behaviour of the authoring routes: shaping a lifecycle from the product.

``TestCase``: these routes write, but they write no ``OutboxEvent`` — a graph nobody is standing on
has nothing to recompute — so there is no ``on_commit`` for a never-committed transaction to hide.

The graphs are built here in Python rather than loaded from the seed fixtures, for the same reason
:mod:`apps.workflow.tests.api.test_workflow_routes` builds its own: the fixtures are the operation's
real data, and an assertion about *which* states come back would fail the day somebody inserts one.

Four rules are what make this surface safe to expose, and each has a class of its own:

* :class:`RetiringAStateNeverDeletesItTestCase` — nothing is deleted while it is in use, and the
  refusal names how many records are in the way.
* :class:`GraphCoherenceTestCase` — an edge joins two states of *one* graph, a state code is unique
  inside its graph, and the closed vocabularies are closed.
* :class:`TerminalStateTestCase` — withdrawing the last move out of a state is allowed, because that
  is how an end of the lifecycle is declared.
* :class:`StateOrderTestCase` — ``order`` is the operator's arrangement, so a new column appends.

Plus the two that hold the whole thing up: every write is an ops lead's
(:class:`AuthoringPermissionTestCase`), and every write is in the trail
(:class:`AuthoringTrailTestCase`).
"""

from datetime import date
from typing import Any

from django.test import SimpleTestCase, TestCase
from pydantic import ValidationError

from apps.accounts.tests.support import bearer, make_member
from apps.activity.models import ActivityRecord
from apps.catalog.models import Currency, EngagementType
from apps.portfolio.models import Client, Project
from apps.workflow.domain.commands import UpdateStateCommand, UpdateTransitionCommand
from apps.workflow.domain.errors import WorkflowStateInUse
from apps.workflow.models import (
    AppliesTo,
    StateCategory,
    Workflow,
    WorkflowBinding,
    WorkflowState,
    WorkflowTransition,
)

WORKFLOWS_URL = "/api/v1/workflows"

#: The ops lead every authoring request signs in as.
LEAD_CODE = "camila.torres"

#: A member who is signed in and is not an ops lead. Reading the graphs is theirs; shaping them is
#: not, and the difference is the entire authorization model of this feature.
MEMBER_CODE = "diego.rojas"

#: The graph the editing tests reshape.
FLOW_CODE = "test-authoring-flow"

#: ``order`` the two seeded columns are created with. Deliberately not 0 and 1, so an appended
#: column landing at "one past the highest" is distinguishable from one landing at "count".
BACKLOG_ORDER = 4
IN_PROGRESS_ORDER = 9


def _build_graph() -> Workflow:
    """Create the graph the authoring tests edit: two columns, one arrow between them."""
    workflow = Workflow.objects.create(
        code=FLOW_CODE,
        name="Ciclo de autoría",
        applies_to=AppliesTo.PROJECT,
    )
    backlog = WorkflowState.objects.create(
        workflow=workflow,
        code="descubrimiento",
        label="Descubrimiento",
        category=StateCategory.BACKLOG,
        is_initial=True,
        order=BACKLOG_ORDER,
    )
    in_progress = WorkflowState.objects.create(
        workflow=workflow,
        code="ejecucion",
        label="Ejecución",
        category=StateCategory.IN_PROGRESS,
        order=IN_PROGRESS_ORDER,
    )
    WorkflowTransition.objects.create(
        workflow=workflow,
        from_state=backlog,
        to_state=in_progress,
        label="Iniciar ejecución",
        order=0,
    )
    return workflow


def _place_project_on(state: WorkflowState, *, code: str = "PRJ-A1") -> Project:
    """Park one project on a state, so retiring that column would strand a record."""
    engagement_type = EngagementType.objects.create(code="diagnostico", label="Diagnóstico")
    currency = Currency.objects.create(code="CLP", label="Peso chileno")
    client = Client.objects.create(code="ACME", alias="Acme")
    return Project.objects.create(
        code=code,
        name="Proyecto de prueba",
        client=client,
        engagement_type=engagement_type,
        workflow_state=state,
        currency=currency,
        target_date=date(2026, 12, 31),
    )


class AuthoringSurface:
    """The eight writes, as one signed-in client issues them. Shared by the classes below."""

    client: Any
    auth: dict[str, str]

    def post_workflow(self, **body: Any) -> Any:
        return self.client.post(
            WORKFLOWS_URL, data=body, content_type="application/json", **self.auth
        )

    def patch_workflow(self, workflow: str, **body: Any) -> Any:
        return self.client.patch(
            f"{WORKFLOWS_URL}/{workflow}", data=body, content_type="application/json", **self.auth
        )

    def post_state(self, workflow: str, **body: Any) -> Any:
        return self.client.post(
            f"{WORKFLOWS_URL}/{workflow}/states",
            data=body,
            content_type="application/json",
            **self.auth,
        )

    def patch_state(self, workflow: str, state_code: str, **body: Any) -> Any:
        return self.client.patch(
            f"{WORKFLOWS_URL}/{workflow}/states/{state_code}",
            data=body,
            content_type="application/json",
            **self.auth,
        )

    def delete_state(self, workflow: str, state_code: str) -> Any:
        return self.client.delete(f"{WORKFLOWS_URL}/{workflow}/states/{state_code}", **self.auth)

    def post_transition(self, workflow: str, **body: Any) -> Any:
        return self.client.post(
            f"{WORKFLOWS_URL}/{workflow}/transitions",
            data=body,
            content_type="application/json",
            **self.auth,
        )

    def patch_transition(self, workflow: str, from_state: str, to_state: str, **body: Any) -> Any:
        return self.client.patch(
            f"{WORKFLOWS_URL}/{workflow}/transitions/{from_state}/{to_state}",
            data=body,
            content_type="application/json",
            **self.auth,
        )

    def delete_transition(self, workflow: str, from_state: str, to_state: str) -> Any:
        return self.client.delete(
            f"{WORKFLOWS_URL}/{workflow}/transitions/{from_state}/{to_state}", **self.auth
        )

    def graph(self, workflow_code: str) -> Any:
        """The graph as the read document publishes it, which is also what every write returns."""
        body = self.client.get(WORKFLOWS_URL, **self.auth).json()
        return next(w for w in body["workflows"] if w["code"] == workflow_code)


class CreateWorkflowTestCase(AuthoringSurface, TestCase):
    """A lifecycle can be defined from the product: empty, active, governing one kind of work."""

    @classmethod
    def setUpTestData(cls) -> None:
        make_member(code=LEAD_CODE, is_ops_lead=True)
        EngagementType.objects.create(code="diagnostico", label="Diagnóstico")

    def setUp(self) -> None:
        self.auth = bearer(username=LEAD_CODE)

    def test_creating_a_workflow_answers_201_with_the_empty_graph(self) -> None:
        response = self.post_workflow(
            code="ciclo-soporte", name="Ciclo de soporte", applies_to="PROJECT"
        )

        self.assertEqual(response.status_code, 201)
        self.assertEqual(
            response.json(),
            {
                "code": "ciclo-soporte",
                "name": "Ciclo de soporte",
                "applies_to": "PROJECT",
                "is_default": False,
                "is_active": True,
                "engagement_types": [],
                "states": [],
                "transitions": [],
            },
        )

    def test_a_new_workflow_never_becomes_the_default_for_its_kind(self) -> None:
        """The fallback decides what *unbound* work follows; adding a lifecycle must not move it."""
        self.post_workflow(code="ciclo-soporte", name="Ciclo de soporte", applies_to="PROJECT")

        self.assertFalse(Workflow.objects.get(code="ciclo-soporte").is_default)

    def test_engagement_types_are_bound_so_work_of_that_type_resolves_to_the_graph(self) -> None:
        response = self.post_workflow(
            code="ciclo-diag",
            name="Ciclo de diagnóstico",
            applies_to="PROJECT",
            engagement_types=["diagnostico"],
        )

        self.assertEqual(
            response.json()["engagement_types"],
            [{"code": "diagnostico", "label": "Diagnóstico", "color": None}],
        )
        self.assertEqual(
            Workflow.objects.resolve(
                applies_to=AppliesTo.PROJECT,
                engagement_type_id=EngagementType.objects.get(code="diagnostico").pk,
            ).code,
            "ciclo-diag",
        )

    def test_a_code_already_taken_is_refused_as_a_conflict(self) -> None:
        self.post_workflow(code="ciclo-soporte", name="Ciclo de soporte", applies_to="PROJECT")

        response = self.post_workflow(code="ciclo-soporte", name="Otro ciclo", applies_to="PROJECT")

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["code"], "conflicting_state")

    def test_an_entity_kind_outside_project_and_task_is_refused_naming_both(self) -> None:
        response = self.post_workflow(code="ciclo-x", name="Ciclo X", applies_to="CLIENT")

        self.assertEqual(response.status_code, 422)
        body = response.json()
        self.assertIn("applies_to", body["details"]["fields"])
        self.assertEqual(sorted(body["details"]["allowed"]), ["PROJECT", "TASK"])

    def test_an_engagement_type_bound_elsewhere_is_refused_naming_the_graph_that_holds_it(
        self,
    ) -> None:
        """Resolution has to be deterministic: one lifecycle per engagement type per kind."""
        self.post_workflow(
            code="ciclo-uno",
            name="Ciclo uno",
            applies_to="PROJECT",
            engagement_types=["diagnostico"],
        )

        response = self.post_workflow(
            code="ciclo-dos",
            name="Ciclo dos",
            applies_to="PROJECT",
            engagement_types=["diagnostico"],
        )

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["details"]["workflow"], "ciclo-uno")

    def test_an_unknown_engagement_type_is_a_field_error_and_creates_nothing(self) -> None:
        response = self.post_workflow(
            code="ciclo-tres",
            name="Ciclo tres",
            applies_to="PROJECT",
            engagement_types=["no-existe"],
        )

        self.assertEqual(response.status_code, 422)
        self.assertIn("engagement_types", response.json()["details"]["fields"])
        self.assertFalse(Workflow.objects.filter(code="ciclo-tres").exists())


class WorkflowHeaderEditTestCase(AuthoringSurface, TestCase):
    """Renaming and retiring a lifecycle, and the no-op that writes nothing."""

    @classmethod
    def setUpTestData(cls) -> None:
        make_member(code=LEAD_CODE, is_ops_lead=True)
        _build_graph()

    def setUp(self) -> None:
        self.auth = bearer(username=LEAD_CODE)

    def test_renaming_changes_the_name_and_not_the_code(self) -> None:
        response = self.patch_workflow(FLOW_CODE, name="Ciclo renombrado")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["name"], "Ciclo renombrado")
        self.assertEqual(response.json()["code"], FLOW_CODE)

    def test_deactivating_retires_the_graph_and_keeps_the_row(self) -> None:
        response = self.patch_workflow(FLOW_CODE, is_active=False)

        self.assertFalse(response.json()["is_active"])
        self.assertTrue(Workflow.objects.filter(code=FLOW_CODE).exists())

    def test_a_retired_graph_is_still_served_so_records_on_it_keep_their_columns(self) -> None:
        self.patch_workflow(FLOW_CODE, is_active=False)

        self.assertFalse(self.graph(FLOW_CODE)["is_active"])

    def test_reactivating_puts_it_back(self) -> None:
        self.patch_workflow(FLOW_CODE, is_active=False)

        self.assertTrue(self.patch_workflow(FLOW_CODE, is_active=True).json()["is_active"])

    def test_sending_the_current_values_writes_no_trail_entry(self) -> None:
        """A trail that recorded non-changes could not be read for changes."""
        self.patch_workflow(FLOW_CODE, name="Ciclo de autoría", is_active=True)

        self.assertEqual(ActivityRecord.objects.filter(entity_type="workflow").count(), 0)

    def test_editing_a_workflow_that_does_not_exist_is_404(self) -> None:
        self.assertEqual(self.patch_workflow("no-existe", name="X").status_code, 404)


class StateOrderTestCase(AuthoringSurface, TestCase):
    """``order`` is the operator's arrangement, so a new column appends instead of landing at 0."""

    @classmethod
    def setUpTestData(cls) -> None:
        make_member(code=LEAD_CODE, is_ops_lead=True)
        _build_graph()

    def setUp(self) -> None:
        self.auth = bearer(username=LEAD_CODE)

    def test_a_state_created_without_an_order_is_appended_after_the_last_column(self) -> None:
        response = self.post_state(FLOW_CODE, code="entregado", label="Entregado", category="DONE")

        added = next(s for s in response.json()["states"] if s["code"] == "entregado")
        self.assertEqual(added["order"], IN_PROGRESS_ORDER + 1)

    def test_an_appended_state_is_last_in_the_document_and_not_first(self) -> None:
        self.post_state(FLOW_CODE, code="entregado", label="Entregado", category="DONE")

        self.assertEqual(
            [state["code"] for state in self.graph(FLOW_CODE)["states"]],
            ["descubrimiento", "ejecucion", "entregado"],
        )

    def test_an_explicit_order_is_honoured(self) -> None:
        response = self.post_state(
            FLOW_CODE, code="pausado", label="Pausado", category="BACKLOG", order=0
        )

        self.assertEqual(response.json()["states"][0]["code"], "pausado")

    def test_the_first_state_of_an_empty_graph_becomes_its_entry_node(self) -> None:
        """Otherwise a graph authored entirely from the product could hold no new work."""
        self.post_workflow(code="ciclo-vacio", name="Ciclo vacío", applies_to="TASK")

        response = self.post_state("ciclo-vacio", code="nuevo", label="Nuevo", category="BACKLOG")

        self.assertTrue(response.json()["states"][0]["is_initial"])

    def test_a_later_state_is_not_an_entry_node(self) -> None:
        response = self.post_state(FLOW_CODE, code="entregado", label="Entregado", category="DONE")

        added = next(s for s in response.json()["states"] if s["code"] == "entregado")
        self.assertFalse(added["is_initial"])


class GraphCoherenceTestCase(AuthoringSurface, TestCase):
    """A graph stays internally consistent: its codes, its endpoints and its vocabularies."""

    @classmethod
    def setUpTestData(cls) -> None:
        make_member(code=LEAD_CODE, is_ops_lead=True)
        _build_graph()
        other = Workflow.objects.create(
            code="otro-ciclo", name="Otro ciclo", applies_to=AppliesTo.PROJECT
        )
        WorkflowState.objects.create(
            workflow=other,
            code="ajeno",
            label="Ajeno",
            category=StateCategory.BACKLOG,
            is_initial=True,
        )

    def setUp(self) -> None:
        self.auth = bearer(username=LEAD_CODE)

    def test_a_state_code_is_unique_inside_its_graph(self) -> None:
        response = self.post_state(
            FLOW_CODE, code="ejecucion", label="Otra ejecución", category="IN_PROGRESS"
        )

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["details"]["id"], "ejecucion")

    def test_the_same_state_code_may_exist_in_another_graph(self) -> None:
        """``bloqueada`` in two lifecycles is legal, which is why nothing branches on a code."""
        response = self.post_state(
            "otro-ciclo", code="ejecucion", label="Ejecución", category="IN_PROGRESS"
        )

        self.assertEqual(response.status_code, 201)

    def test_a_category_outside_the_five_is_refused_naming_them(self) -> None:
        response = self.post_state(
            FLOW_CODE, code="revision", label="Revisión", category="REVIEWING"
        )

        self.assertEqual(response.status_code, 422)
        self.assertIn("category", response.json()["details"]["fields"])
        self.assertIn("BLOCKED", response.json()["details"]["allowed"])

    def test_a_transition_cannot_reach_a_state_of_another_graph(self) -> None:
        """The endpoint is resolved inside the graph in the path, so ``ajeno`` is simply not there."""
        response = self.post_transition(
            FLOW_CODE, from_state="ejecucion", to_state="ajeno", label="Saltar de ciclo"
        )

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["details"]["entity"], "workflow_state")
        self.assertEqual(
            WorkflowTransition.objects.filter(to_state__code="ajeno").count(),
            0,
        )

    def test_a_second_edge_for_the_same_ordered_pair_is_refused(self) -> None:
        response = self.post_transition(
            FLOW_CODE, from_state="descubrimiento", to_state="ejecucion", label="Otra vez"
        )

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["details"]["id"], "descubrimiento->ejecucion")

    def test_a_guard_nothing_registered_is_refused_at_authoring_time(self) -> None:
        """A typo must not sit in the graph claiming to enforce a check that evaporated."""
        response = self.post_transition(
            FLOW_CODE,
            from_state="ejecucion",
            to_state="descubrimiento",
            label="Devolver",
            guard="no_such_guard",
        )

        self.assertEqual(response.status_code, 422)
        self.assertIn("guard", response.json()["details"]["fields"])

    def test_a_registered_guard_is_accepted_and_stays_off_the_document(self) -> None:
        response = self.post_transition(
            FLOW_CODE,
            from_state="ejecucion",
            to_state="descubrimiento",
            label="Devolver",
            guard="require_open_blocker",
        )

        self.assertEqual(response.status_code, 201)
        self.assertEqual(
            WorkflowTransition.objects.get(to_state__code="descubrimiento").guard,
            "require_open_blocker",
        )
        self.assertNotIn("guard", response.content.decode())

    def test_the_guard_picker_is_served_from_the_registry(self) -> None:
        response = self.client.get(f"{WORKFLOWS_URL}/guards", **self.auth)

        self.assertEqual(response.status_code, 200)
        self.assertIn("require_open_blocker", response.json())

    def test_an_edge_lands_on_the_graph_document_with_both_endpoints(self) -> None:
        self.post_transition(
            FLOW_CODE, from_state="ejecucion", to_state="descubrimiento", label="Devolver"
        )

        self.assertIn(
            ("ejecucion", "descubrimiento"),
            {(e["from_state"], e["to_state"]) for e in self.graph(FLOW_CODE)["transitions"]},
        )


class RetiringAStateNeverDeletesItTestCase(AuthoringSurface, TestCase):
    """Rule one: nothing is deleted while it is in use, and the refusal says what is in the way."""

    @classmethod
    def setUpTestData(cls) -> None:
        make_member(code=LEAD_CODE, is_ops_lead=True)
        _build_graph()

    def setUp(self) -> None:
        self.auth = bearer(username=LEAD_CODE)

    def test_retiring_an_unoccupied_state_keeps_the_row_and_flags_it(self) -> None:
        response = self.delete_state(FLOW_CODE, "ejecucion")

        self.assertEqual(response.status_code, 200)
        self.assertTrue(WorkflowState.objects.filter(code="ejecucion").exists())
        self.assertFalse(WorkflowState.objects.get(code="ejecucion").is_active)

    def test_a_retired_state_is_still_published_flagged_so_a_board_can_draw_it(self) -> None:
        self.delete_state(FLOW_CODE, "ejecucion")

        retired = next(s for s in self.graph(FLOW_CODE)["states"] if s["code"] == "ejecucion")
        self.assertFalse(retired["is_active"])
        self.assertFalse(retired["can_retire"])

    def test_retiring_a_state_withdraws_every_arrow_touching_it(self) -> None:
        """An arrow into a column outside the graph is a move nothing may take."""
        self.delete_state(FLOW_CODE, "ejecucion")

        self.assertEqual(self.graph(FLOW_CODE)["transitions"], [])
        self.assertFalse(WorkflowTransition.objects.get(to_state__code="ejecucion").is_active)

    def test_retiring_a_state_records_still_occupy_is_refused_with_the_count(self) -> None:
        _place_project_on(WorkflowState.objects.get(code="ejecucion"))

        response = self.delete_state(FLOW_CODE, "ejecucion")

        self.assertEqual(response.status_code, 409)
        details = response.json()["details"]
        self.assertEqual(details["current"], "occupied")
        self.assertEqual(details["records"], 1)
        self.assertEqual(details["projects"], 1)
        self.assertEqual(details["tasks"], 0)

    def test_a_refused_retirement_changes_nothing_at_all(self) -> None:
        _place_project_on(WorkflowState.objects.get(code="ejecucion"))

        self.delete_state(FLOW_CODE, "ejecucion")

        self.assertTrue(WorkflowState.objects.get(code="ejecucion").is_active)
        self.assertTrue(WorkflowTransition.objects.get(to_state__code="ejecucion").is_active)

    def test_the_document_says_which_columns_may_be_retired_and_how_full_they_are(self) -> None:
        """The editor renders ``can_retire`` rather than deriving a rule the server owns."""
        _place_project_on(WorkflowState.objects.get(code="ejecucion"))

        by_code = {state["code"]: state for state in self.graph(FLOW_CODE)["states"]}
        self.assertEqual(by_code["ejecucion"]["record_count"], 1)
        self.assertFalse(by_code["ejecucion"]["can_retire"])
        self.assertEqual(by_code["descubrimiento"]["record_count"], 0)
        self.assertTrue(by_code["descubrimiento"]["can_retire"])

    def test_retiring_a_state_of_another_graph_is_not_found(self) -> None:
        response = self.delete_state(FLOW_CODE, "no-existe")

        self.assertEqual(response.status_code, 404)

    def test_a_retired_state_can_be_restored_through_patch(self) -> None:
        """A retire button with no way back is a delete with better manners."""
        self.delete_state(FLOW_CODE, "ejecucion")

        response = self.patch_state(FLOW_CODE, "ejecucion", is_active=True)

        restored = next(s for s in response.json()["states"] if s["code"] == "ejecucion")
        self.assertTrue(restored["is_active"])

    def test_patch_cannot_retire_a_state_behind_the_occupancy_check(self) -> None:
        """``is_active: false`` is unrepresentable here, so the rule has exactly one door."""
        _place_project_on(WorkflowState.objects.get(code="ejecucion"))

        response = self.patch_state(FLOW_CODE, "ejecucion", is_active=False)

        self.assertEqual(response.status_code, 422)
        self.assertTrue(WorkflowState.objects.get(code="ejecucion").is_active)


class TerminalStateTestCase(AuthoringSurface, TestCase):
    """Rule three's other half: withdrawing the last move out of a state declares an end."""

    @classmethod
    def setUpTestData(cls) -> None:
        make_member(code=LEAD_CODE, is_ops_lead=True)
        _build_graph()

    def setUp(self) -> None:
        self.auth = bearer(username=LEAD_CODE)

    def test_withdrawing_the_only_move_out_of_a_state_is_allowed(self) -> None:
        response = self.delete_transition(FLOW_CODE, "descubrimiento", "ejecucion")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["transitions"], [])

    def test_a_withdrawn_edge_keeps_its_row_because_history_names_it(self) -> None:
        self.delete_transition(FLOW_CODE, "descubrimiento", "ejecucion")

        edge = WorkflowTransition.objects.get(from_state__code="descubrimiento")
        self.assertFalse(edge.is_active)

    def test_both_endpoints_survive_the_withdrawal_untouched(self) -> None:
        self.delete_transition(FLOW_CODE, "descubrimiento", "ejecucion")

        self.assertEqual(
            [state["code"] for state in self.graph(FLOW_CODE)["states"]],
            ["descubrimiento", "ejecucion"],
        )

    def test_a_withdrawn_move_can_be_re_declared_by_restoring_the_row(self) -> None:
        """A second row for the same pair is forbidden, so ``POST`` is not the way back."""
        self.delete_transition(FLOW_CODE, "descubrimiento", "ejecucion")

        conflict = self.post_transition(
            FLOW_CODE, from_state="descubrimiento", to_state="ejecucion", label="Iniciar"
        )
        restored = self.patch_transition(FLOW_CODE, "descubrimiento", "ejecucion", is_active=True)

        self.assertEqual(conflict.status_code, 409)
        self.assertEqual(len(restored.json()["transitions"]), 1)

    def test_withdrawing_a_move_that_was_never_declared_is_not_found(self) -> None:
        response = self.delete_transition(FLOW_CODE, "ejecucion", "descubrimiento")

        self.assertEqual(response.status_code, 404)


class TransitionEditTestCase(AuthoringSurface, TestCase):
    """What a declared move asks for is editable; what it joins is not."""

    @classmethod
    def setUpTestData(cls) -> None:
        make_member(code=LEAD_CODE, is_ops_lead=True)
        _build_graph()

    def setUp(self) -> None:
        self.auth = bearer(username=LEAD_CODE)

    def test_editing_the_label_and_the_requirements_lands_on_the_document(self) -> None:
        response = self.patch_transition(
            FLOW_CODE,
            "descubrimiento",
            "ejecucion",
            label="Arrancar",
            requires_reason=True,
            requires_fields=["next_step"],
        )

        self.assertEqual(
            response.json()["transitions"][0],
            {
                "from_state": "descubrimiento",
                "to_state": "ejecucion",
                "label": "Arrancar",
                "requires_reason": True,
                "requires_fields": ["next_step"],
            },
        )

    def test_a_blank_entry_in_requires_fields_is_dropped_rather_than_stored(self) -> None:
        self.patch_transition(
            FLOW_CODE, "descubrimiento", "ejecucion", requires_fields=["next_step", "  "]
        )

        self.assertEqual(
            WorkflowTransition.objects.get(from_state__code="descubrimiento").requires_fields,
            ["next_step"],
        )

    def test_sending_the_current_values_writes_no_trail_entry(self) -> None:
        self.patch_transition(FLOW_CODE, "descubrimiento", "ejecucion", label="Iniciar ejecución")

        self.assertEqual(ActivityRecord.objects.filter(verb="TRANSITION_EDITED").count(), 0)

    def test_editing_a_move_that_does_not_exist_is_404(self) -> None:
        response = self.patch_transition(FLOW_CODE, "ejecucion", "descubrimiento", label="X")

        self.assertEqual(response.status_code, 404)


class AuthoringTrailTestCase(AuthoringSurface, TestCase):
    """Every write records what changed, under the graph it changed."""

    @classmethod
    def setUpTestData(cls) -> None:
        make_member(code=LEAD_CODE, is_ops_lead=True)
        _build_graph()

    def setUp(self) -> None:
        self.auth = bearer(username=LEAD_CODE)

    def _records(self, verb: str) -> list[ActivityRecord]:
        return list(ActivityRecord.objects.filter(verb=verb, entity_type="workflow"))

    def test_creating_a_workflow_is_recorded_against_its_own_code(self) -> None:
        self.post_workflow(code="ciclo-nuevo", name="Ciclo nuevo", applies_to="TASK")

        record = self._records("CREATED")[0]
        self.assertEqual(record.entity_id, "ciclo-nuevo")
        self.assertEqual(record.actor, LEAD_CODE)
        self.assertEqual(record.to_value, "Ciclo nuevo")

    def test_adding_a_state_names_the_state_in_the_metadata(self) -> None:
        self.post_state(FLOW_CODE, code="entregado", label="Entregado", category="DONE")

        record = self._records("STATE_ADDED")[0]
        self.assertEqual(record.entity_id, FLOW_CODE)
        self.assertEqual(record.metadata["state"], "entregado")

    def test_editing_a_state_records_one_entry_naming_every_field_that_moved(self) -> None:
        """One act of authoring, one entry: four verbs would report four decisions."""
        self.patch_state(FLOW_CODE, "ejecucion", label="En ejecución", color="#123456")

        record = self._records("STATE_EDITED")[0]
        self.assertEqual(record.from_value, "Ejecución")
        self.assertEqual(record.to_value, "En ejecución")
        self.assertEqual(
            record.metadata["changed"],
            {
                "label": {"from": "Ejecución", "to": "En ejecución"},
                "color": {"from": "", "to": "#123456"},
            },
        )

    def test_retiring_a_state_records_the_arrows_it_withdrew_in_the_same_decision(self) -> None:
        self.delete_state(FLOW_CODE, "ejecucion")

        record = self._records("STATE_RETIRED")[0]
        self.assertEqual(record.to_value, "retired")
        self.assertEqual(
            record.metadata["withdrawn_transitions"],
            [{"from": "descubrimiento", "to": "ejecucion"}],
        )

    def test_declaring_and_withdrawing_a_move_are_both_recorded(self) -> None:
        self.post_transition(
            FLOW_CODE, from_state="ejecucion", to_state="descubrimiento", label="Devolver"
        )
        self.delete_transition(FLOW_CODE, "ejecucion", "descubrimiento")

        self.assertEqual(
            self._records("TRANSITION_ADDED")[0].to_value, "ejecucion -> descubrimiento"
        )
        self.assertEqual(
            self._records("TRANSITION_RETIRED")[0].metadata["to_state"], "descubrimiento"
        )

    def test_one_request_writes_one_correlation_id(self) -> None:
        """Everything a single decision changed reads back as that one decision."""
        self.patch_workflow(FLOW_CODE, name="Ciclo renombrado", is_active=False)

        correlations = {record.correlation_id for record in self._records("RENAMED")} | {
            record.correlation_id for record in self._records("DEACTIVATED")
        }
        self.assertEqual(len(correlations), 1)


class AuthoringPermissionTestCase(AuthoringSurface, TestCase):
    """Reading a lifecycle is any member's; shaping one is an ops lead's, on every route."""

    @classmethod
    def setUpTestData(cls) -> None:
        make_member(code=LEAD_CODE, is_ops_lead=True)
        make_member(code=MEMBER_CODE)
        _build_graph()

    def setUp(self) -> None:
        self.auth = bearer(username=MEMBER_CODE)

    def test_a_member_may_still_read_the_graphs(self) -> None:
        self.assertEqual(self.client.get(WORKFLOWS_URL, **self.auth).status_code, 200)

    def test_every_write_refuses_a_member_who_is_not_an_ops_lead(self) -> None:
        responses = [
            self.post_workflow(code="x", name="X", applies_to="PROJECT"),
            self.patch_workflow(FLOW_CODE, name="X"),
            self.post_state(FLOW_CODE, code="x", label="X", category="DONE"),
            self.patch_state(FLOW_CODE, "ejecucion", label="X"),
            self.delete_state(FLOW_CODE, "ejecucion"),
            self.post_transition(
                FLOW_CODE, from_state="ejecucion", to_state="descubrimiento", label="X"
            ),
            self.patch_transition(FLOW_CODE, "descubrimiento", "ejecucion", label="X"),
            self.delete_transition(FLOW_CODE, "descubrimiento", "ejecucion"),
        ]

        self.assertEqual([response.status_code for response in responses], [403] * 8)

    def test_the_refusal_names_the_capability_the_ui_branches_on(self) -> None:
        body = self.post_state(FLOW_CODE, code="x", label="X", category="DONE").json()

        self.assertEqual(body["code"], "permission_denied")
        self.assertEqual(body["details"]["required"], "ops_lead")

    def test_a_refused_write_changes_nothing(self) -> None:
        self.delete_state(FLOW_CODE, "ejecucion")

        self.assertTrue(WorkflowState.objects.get(code="ejecucion").is_active)

    def test_the_guard_picker_is_not_a_readers_business(self) -> None:
        self.assertEqual(self.client.get(f"{WORKFLOWS_URL}/guards", **self.auth).status_code, 403)

    def test_an_unauthenticated_write_is_401(self) -> None:
        response = self.client.post(
            WORKFLOWS_URL,
            data={"code": "x", "name": "X", "applies_to": "PROJECT"},
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 401)


class AuthoringDoesNotDecideLegalityTestCase(AuthoringSurface, TestCase):
    """The load-bearing separation: authoring writes the graph, it never grants a move.

    Asserted as both halves at once — the edge *is* declared, and the record is *still* refused —
    because either half alone is satisfiable by a mistake.
    """

    @classmethod
    def setUpTestData(cls) -> None:
        make_member(code=LEAD_CODE, is_ops_lead=True)
        _build_graph()

    def setUp(self) -> None:
        self.auth = bearer(username=LEAD_CODE)

    def test_declaring_an_edge_does_not_move_any_record(self) -> None:
        project = _place_project_on(WorkflowState.objects.get(code="descubrimiento"))

        self.post_transition(
            FLOW_CODE, from_state="ejecucion", to_state="descubrimiento", label="Devolver"
        )

        project.refresh_from_db()
        self.assertEqual(project.workflow_state.code, "descubrimiento")

    def test_a_declared_edge_is_not_a_move_the_record_may_take_from_where_it_stands(self) -> None:
        project = _place_project_on(WorkflowState.objects.get(code="descubrimiento"))
        self.post_transition(
            FLOW_CODE, from_state="ejecucion", to_state="descubrimiento", label="Devolver"
        )

        response = self.client.post(
            f"/api/v1/projects/{project.code}/transition",
            data={"to_state": "descubrimiento", "reason": "probando"},
            content_type="application/json",
            **self.auth,
        )

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["code"], "transition_not_allowed")


class AuthoringCommandTestCase(SimpleTestCase):
    """What the commands refuse before any database is involved. No database here at all."""

    def test_a_state_cannot_be_retired_through_the_update_command(self) -> None:
        """``Literal[True]`` is what makes "retire behind the occupancy check" unrepresentable."""
        with self.assertRaises(ValidationError):
            UpdateStateCommand(workflow_code="w", code="s", is_active=False)  # type: ignore[arg-type]

    def test_a_transition_cannot_be_withdrawn_through_the_update_command(self) -> None:
        with self.assertRaises(ValidationError):
            UpdateTransitionCommand(  # type: ignore[arg-type]
                workflow_code="w", from_state="a", to_state="b", is_active=False
            )

    def test_a_colour_that_is_not_a_hex_triplet_is_refused(self) -> None:
        with self.assertRaises(ValidationError):
            UpdateStateCommand(workflow_code="w", code="s", color="rojo")

    def test_restoring_is_expressible(self) -> None:
        command = UpdateStateCommand(workflow_code="w", code="s", is_active=True)

        self.assertTrue(command.is_active)


class StateInUseErrorTestCase(SimpleTestCase):
    """The refusal carries the numbers an operator needs before they can act on it."""

    def test_the_message_names_the_state_and_both_counts(self) -> None:
        error = WorkflowStateInUse("ciclo", "ejecucion", projects=3, tasks=1)

        self.assertIn("ejecucion", str(error))
        self.assertIn("3", str(error))
        self.assertIn("1", str(error))

    def test_records_is_the_total_that_has_to_move(self) -> None:
        self.assertEqual(WorkflowStateInUse("c", "s", projects=3, tasks=1).records, 4)


class WorkflowBindingIsUntouchedByStateEditsTestCase(TestCase):
    """Editing a graph's columns never disturbs which engagement types resolve to it."""

    @classmethod
    def setUpTestData(cls) -> None:
        make_member(code=LEAD_CODE, is_ops_lead=True)
        workflow = _build_graph()
        WorkflowBinding.objects.create(
            workflow=workflow,
            applies_to=AppliesTo.PROJECT,
            engagement_type=EngagementType.objects.create(code="soporte", label="Soporte"),
        )

    def setUp(self) -> None:
        self.auth = bearer(username=LEAD_CODE)

    def test_the_binding_still_resolves_after_a_column_is_retired(self) -> None:
        self.client.delete(f"{WORKFLOWS_URL}/{FLOW_CODE}/states/ejecucion", **self.auth)

        self.assertEqual(
            Workflow.objects.resolve(
                applies_to=AppliesTo.PROJECT,
                engagement_type_id=EngagementType.objects.get(code="soporte").pk,
            ).code,
            FLOW_CODE,
        )
