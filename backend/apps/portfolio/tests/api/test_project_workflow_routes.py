"""HTTP behaviour of putting one project on a lifecycle of its own.

The hard case is the reason this file exists, and it has a class to itself
(:class:`IncompatibleReassignmentTestCase`): a project standing on a state the target graph does not
contain is refused, loudly and with the states the target *does* offer, because the alternative —
landing it anywhere — leaves a project on a state its own workflow does not know about. Every other
class here defends one of the guarantees around that refusal: the reassignment lands cleanly and
keeps the state code, the legal moves afterwards come from the **new** graph, an untouched project
still resolves exactly as it did before this feature existed, and the write is an ops lead's.

``TestCase`` throughout except :class:`ProjectWorkflowOutboxTestCase`, which is the one assertion
``TestCase`` cannot make (CLAUDE.md rule 15).

The second graph is built here in Python rather than loaded from the seed fixtures, for the reason
every other workflow test gives: the fixtures are the operation's real data, and an assertion about
which states exist would fail the day somebody inserts one.
"""

import json
from typing import Any

from django.test import TestCase, TransactionTestCase

from apps.accounts.tests.support import bearer, make_member
from apps.activity.models import ActivityRecord
from apps.events.models import OutboxEvent
from apps.portfolio.models import Project
from apps.portfolio.tests.scenario import (
    OPS_LEAD_CODE,
    OWNER_CODE,
    PROJECT_CODE,
    PortfolioScenario,
)
from apps.workflow.models import (
    AppliesTo,
    StateCategory,
    Workflow,
    WorkflowState,
    WorkflowTransition,
)

#: The lifecycle the scenario's projects are reassigned onto. It shares ``execution`` with the
#: scenario's default graph — which is what makes a clean landing possible — and deliberately has no
#: ``blocked``, which is what makes the incompatible case reachable without inventing a state nobody
#: would configure.
SHORT_FLOW = "ciclo-corto"

#: The scenario's own graph, the one every project inherits.
DEFAULT_FLOW = "test-project-flow"

#: A member who is signed in and is not an ops lead. Reading which lifecycle a project follows is
#: theirs; deciding it is not.
MEMBER_CODE = "diego.rojas"


def _short_flow() -> Workflow:
    """Build the second project lifecycle: ``execution`` → ``entregado``, and nothing else.

    ``execution`` carries the same code as the scenario graph's in-progress state on purpose: a
    reassignment repoints a project at the *equivalent* node, so the two graphs have to agree on
    that code for the move to be possible at all.
    """
    workflow = Workflow.objects.create(
        code=SHORT_FLOW,
        name="Ciclo corto",
        applies_to=AppliesTo.PROJECT,
    )
    execution = WorkflowState.objects.create(
        workflow=workflow,
        code="execution",
        label="Ejecución",
        category=StateCategory.IN_PROGRESS,
        is_initial=True,
        order=0,
    )
    delivered = WorkflowState.objects.create(
        workflow=workflow,
        code="entregado",
        label="Entregado",
        category=StateCategory.DONE,
        order=1,
    )
    WorkflowTransition.objects.create(
        workflow=workflow,
        from_state=execution,
        to_state=delivered,
        label="Entregar",
        order=0,
    )
    return workflow


class ProjectWorkflowSurface:
    """The two writes and the read, as one signed-in client issues them."""

    client: Any
    auth: dict[str, str]

    def assign(self, workflow: str, *, project: str = PROJECT_CODE) -> Any:
        return self.client.put(
            f"/api/v1/projects/{project}/workflow",
            data=json.dumps({"workflow": workflow}),
            content_type="application/json",
            **self.auth,
        )

    def clear(self, *, project: str = PROJECT_CODE) -> Any:
        return self.client.delete(f"/api/v1/projects/{project}/workflow", **self.auth)

    def detail(self, *, project: str = PROJECT_CODE) -> Any:
        return self.client.get(f"/api/v1/projects/{project}", **self.auth).json()


class ProjectFollowsItsOwnWorkflowTestCase(ProjectWorkflowSurface, TestCase):
    """A reassignment that lands cleanly, and what the project obeys afterwards."""

    scenario: PortfolioScenario
    auth: dict[str, str]

    @classmethod
    def setUpTestData(cls) -> None:
        cls.scenario = PortfolioScenario()
        cls.scenario.add_transitions()
        cls.scenario.add_ops_lead()
        _short_flow()
        cls.auth = bearer(username=OPS_LEAD_CODE)

    def test_assigning_a_lifecycle_keeps_the_state_the_project_was_standing_on(self) -> None:
        response = self.assign(SHORT_FLOW)

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["workflow"]["code"], SHORT_FLOW)
        # The whole safety argument in one assertion: a reassignment changes which graph owns the
        # ground, never where the project is standing on it.
        self.assertEqual(body["state"]["code"], "execution")

    def test_the_detail_reports_a_directly_assigned_lifecycle_as_such(self) -> None:
        self.assign(SHORT_FLOW)

        workflow = self.detail()["workflow"]
        self.assertEqual(workflow["source"], "DIRECT")
        self.assertEqual(workflow["name"], "Ciclo corto")

    def test_the_legal_moves_after_a_reassignment_come_from_the_new_graph(self) -> None:
        before = [option["to_state"]["code"] for option in self.detail()["transitions"]]

        self.assign(SHORT_FLOW)
        after = [option["to_state"]["code"] for option in self.detail()["transitions"]]

        self.assertEqual(before, ["blocked"])
        self.assertEqual(after, ["entregado"])

    def test_a_move_the_old_graph_offered_is_refused_once_the_new_one_governs(self) -> None:
        self.assign(SHORT_FLOW)

        response = self.client.post(
            f"/api/v1/projects/{PROJECT_CODE}/transition",
            data=json.dumps({"to_state": "blocked", "reason": "Se cayó el proveedor."}),
            content_type="application/json",
            **self.auth,
        )

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["code"], "transition_not_allowed")
        self.assertEqual(response.json()["details"]["allowed"], ["entregado"])

    def test_a_move_the_new_graph_declares_is_taken_normally(self) -> None:
        self.assign(SHORT_FLOW)

        response = self.client.post(
            f"/api/v1/projects/{PROJECT_CODE}/transition",
            data=json.dumps({"to_state": "entregado"}),
            content_type="application/json",
            **self.auth,
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["state"]["code"], "entregado")

    def test_reassigning_one_project_leaves_its_sibling_where_it_was(self) -> None:
        self.assign(SHORT_FLOW)

        sibling = self.detail(project="PRJ-T2")
        self.assertEqual(sibling["workflow"]["code"], DEFAULT_FLOW)
        self.assertEqual(sibling["workflow"]["source"], "INHERITED")

    def test_clearing_the_assignment_hands_the_project_back_to_its_binding(self) -> None:
        self.assign(SHORT_FLOW)

        response = self.clear()

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["workflow"]["code"], DEFAULT_FLOW)
        self.assertEqual(body["workflow"]["source"], "INHERITED")
        self.assertEqual(
            [option["to_state"]["code"] for option in body["transitions"]], ["blocked"]
        )

    def test_the_column_is_written_and_not_only_reported(self) -> None:
        self.assign(SHORT_FLOW)

        project = Project.objects.get(code=PROJECT_CODE)
        self.assertEqual(project.workflow.code, SHORT_FLOW)
        self.assertEqual(project.workflow_state.workflow_id, project.workflow_id)


class InheritedWorkflowRegressionTestCase(ProjectWorkflowSurface, TestCase):
    """A project nobody assigned resolves exactly as it did before this feature existed."""

    scenario: PortfolioScenario
    auth: dict[str, str]

    @classmethod
    def setUpTestData(cls) -> None:
        cls.scenario = PortfolioScenario()
        cls.scenario.add_transitions()
        cls.auth = bearer(username=OWNER_CODE)

    def test_an_untouched_project_follows_the_graph_its_binding_gives_it(self) -> None:
        body = self.detail()

        self.assertEqual(
            body["workflow"],
            {
                "code": DEFAULT_FLOW,
                "name": "Test project flow",
                "source": "INHERITED",
            },
        )

    def test_an_untouched_project_stores_no_assignment_at_all(self) -> None:
        self.assertIsNone(Project.objects.get(code=PROJECT_CODE).workflow_id)

    def test_its_legal_moves_still_come_from_the_graph_it_inherited(self) -> None:
        self.assertEqual(
            [option["to_state"]["code"] for option in self.detail()["transitions"]], ["blocked"]
        )

    def test_a_newly_created_project_still_starts_on_the_bound_graph_entry_node(self) -> None:
        response = self.client.post(
            "/api/v1/projects",
            data=json.dumps(
                {"name": "Nuevo proyecto", "client": "atlas", "engagement_type": "proyecto"}
            ),
            content_type="application/json",
            **self.auth,
        )

        self.assertEqual(response.status_code, 201)
        body = response.json()
        self.assertEqual(body["state"]["code"], "discovery")
        self.assertEqual(body["workflow"]["source"], "INHERITED")


class IncompatibleReassignmentTestCase(ProjectWorkflowSurface, TestCase):
    """The hard case: the target has no column for where the project is standing.

    Refused rather than landed somewhere. Landing the project on a state the caller picked would
    make this route a second writer of ``workflow_state`` with no edge, no guard and no required
    field behind it — the door CLAUDE.md rule 2 closes — so the operator is told what is in the way
    and given the two honest ways round it.
    """

    scenario: PortfolioScenario
    auth: dict[str, str]

    @classmethod
    def setUpTestData(cls) -> None:
        cls.scenario = PortfolioScenario()
        cls.scenario.add_transitions()
        cls.scenario.add_ops_lead()
        _short_flow()
        cls.auth = bearer(username=OPS_LEAD_CODE)

    def setUp(self) -> None:
        # ``blocked`` exists only in the scenario's graph, so a project standing there has no
        # equivalent node in the short cycle.
        self.scenario.block_project()

    def test_a_project_on_a_state_the_target_lacks_is_refused(self) -> None:
        response = self.assign(SHORT_FLOW)

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["code"], "conflicting_state")

    def test_the_refusal_names_the_state_both_graphs_and_what_the_target_offers(self) -> None:
        details = self.assign(SHORT_FLOW).json()["details"]

        self.assertEqual(details["id"], "blocked")
        self.assertEqual(details["record"], PROJECT_CODE)
        self.assertEqual(details["workflow"], SHORT_FLOW)
        self.assertEqual(details["current_workflow"], DEFAULT_FLOW)
        self.assertEqual(details["current"], "incompatible")
        self.assertEqual(details["available"], ["execution", "entregado"])

    def test_a_refused_reassignment_changes_nothing_at_all(self) -> None:
        self.assign(SHORT_FLOW)

        project = Project.objects.get(code=PROJECT_CODE)
        self.assertIsNone(project.workflow_id)
        self.assertEqual(project.workflow_state.code, "blocked")
        self.assertFalse(
            ActivityRecord.objects.filter(
                entity_id=PROJECT_CODE, verb=ActivityRecord.Verb.WORKFLOW_ASSIGNED
            ).exists()
        )

    def test_adding_the_missing_column_to_the_target_is_a_way_forward(self) -> None:
        self.client.post(
            f"/api/v1/workflows/{SHORT_FLOW}/states",
            data=json.dumps({"code": "blocked", "label": "Bloqueado", "category": "BLOCKED"}),
            content_type="application/json",
            **self.auth,
        )

        response = self.assign(SHORT_FLOW)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["state"]["code"], "blocked")

    def test_a_retired_state_of_the_target_is_not_a_landing_site(self) -> None:
        workflow = Workflow.objects.get(code=SHORT_FLOW)
        WorkflowState.objects.create(
            workflow=workflow,
            code="blocked",
            label="Bloqueado",
            category=StateCategory.BLOCKED,
            is_active=False,
            order=2,
        )

        response = self.assign(SHORT_FLOW)

        self.assertEqual(response.status_code, 409)
        self.assertNotIn("blocked", response.json()["details"]["available"])


class ClearingBackOntoAnIncompatibleGraphTestCase(ProjectWorkflowSurface, TestCase):
    """An inherited graph is not a safe graph: the fallback is checked like any other target."""

    scenario: PortfolioScenario
    auth: dict[str, str]

    @classmethod
    def setUpTestData(cls) -> None:
        cls.scenario = PortfolioScenario()
        cls.scenario.add_transitions()
        cls.scenario.add_ops_lead()
        _short_flow()
        cls.auth = bearer(username=OPS_LEAD_CODE)

    def test_a_project_cannot_be_dropped_back_onto_a_graph_that_lacks_its_state(self) -> None:
        self.assign(SHORT_FLOW)
        self.client.post(
            f"/api/v1/projects/{PROJECT_CODE}/transition",
            data=json.dumps({"to_state": "entregado"}),
            content_type="application/json",
            **self.auth,
        )

        response = self.clear()

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["details"]["id"], "entregado")
        self.assertEqual(response.json()["details"]["workflow"], DEFAULT_FLOW)


class ReassignmentRefusalTestCase(ProjectWorkflowSurface, TestCase):
    """The other four ways a reassignment is refused, each with its own status."""

    scenario: PortfolioScenario
    auth: dict[str, str]

    @classmethod
    def setUpTestData(cls) -> None:
        cls.scenario = PortfolioScenario()
        cls.scenario.add_transitions()
        cls.scenario.add_ops_lead()
        cls.scenario.add_task_workflow()
        _short_flow()
        cls.auth = bearer(username=OPS_LEAD_CODE)

    def test_an_unknown_workflow_is_a_404_naming_it(self) -> None:
        response = self.assign("no-existe")

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["details"]["id"], "no-existe")

    def test_an_unknown_project_is_a_404_naming_the_project(self) -> None:
        response = self.assign(SHORT_FLOW, project="PRJ-NOPE")

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["details"], {"entity": "project", "id": "PRJ-NOPE"})

    def test_a_task_lifecycle_is_refused_for_a_project_naming_the_field(self) -> None:
        response = self.assign("test-task-flow")

        self.assertEqual(response.status_code, 422)
        self.assertIn("workflow", response.json()["details"]["fields"])

    def test_a_retired_lifecycle_takes_no_arrivals(self) -> None:
        Workflow.objects.filter(code=SHORT_FLOW).update(is_active=False)

        response = self.assign(SHORT_FLOW)

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["details"]["current"], "retired")


class ReassignmentPermissionTestCase(ProjectWorkflowSurface, TestCase):
    """Putting one project on a lifecycle is any member's; authoring the lifecycle is not.

    This route used to be ops-lead. What made that untenable is that the same act reaches the
    operation by a second, unguarded door: changing a project's engagement type through
    ``PATCH /projects/{code}`` re-resolves the binding and therefore the graph, and that field is
    any member's. A lock on one of two doors is not a permission model. The gate stays where it
    still means something — authoring a graph, under ``/api/v1/workflows``.
    """

    scenario: PortfolioScenario
    auth: dict[str, str]

    @classmethod
    def setUpTestData(cls) -> None:
        cls.scenario = PortfolioScenario()
        cls.scenario.add_transitions()
        make_member(code=MEMBER_CODE)
        _short_flow()
        cls.auth = bearer(username=MEMBER_CODE)

    def test_a_member_who_is_not_an_ops_lead_may_assign_a_lifecycle(self) -> None:
        response = self.assign(SHORT_FLOW)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["workflow"]["code"], SHORT_FLOW)

    def test_that_member_may_clear_the_assignment_again(self) -> None:
        self.assign(SHORT_FLOW)

        response = self.clear()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["workflow"]["source"], "INHERITED")

    def test_authoring_a_graph_is_still_refused_to_the_same_member(self) -> None:
        """The gate did not disappear, it moved to the act that still deserves it."""
        response = self.client.post(
            "/api/v1/workflows",
            data=json.dumps({"code": "invented", "name": "Inventado", "applies_to": "PROJECT"}),
            content_type="application/json",
            **self.auth,
        )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["details"]["required"], "ops_lead")

    def test_an_unauthenticated_request_is_still_401(self) -> None:
        response = self.client.put(
            f"/api/v1/projects/{PROJECT_CODE}/workflow",
            data=json.dumps({"workflow": SHORT_FLOW}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 401)

    def test_that_member_may_still_read_which_lifecycle_the_project_follows(self) -> None:
        self.assertEqual(self.detail()["workflow"]["code"], DEFAULT_FLOW)


class ReassignmentTrailTestCase(ProjectWorkflowSurface, TestCase):
    """Every reassignment is reconstructable: one record, naming both graphs."""

    scenario: PortfolioScenario
    auth: dict[str, str]

    @classmethod
    def setUpTestData(cls) -> None:
        cls.scenario = PortfolioScenario()
        cls.scenario.add_transitions()
        cls.scenario.add_ops_lead()
        _short_flow()
        cls.auth = bearer(username=OPS_LEAD_CODE)

    def _records(self) -> list[ActivityRecord]:
        return list(
            ActivityRecord.objects.filter(
                entity_id=PROJECT_CODE, verb=ActivityRecord.Verb.WORKFLOW_ASSIGNED
            ).order_by("id")
        )

    def test_the_record_names_the_graph_left_and_the_graph_joined(self) -> None:
        self.assign(SHORT_FLOW)

        record = self._records()[0]
        self.assertEqual(record.from_value, DEFAULT_FLOW)
        self.assertEqual(record.to_value, SHORT_FLOW)
        self.assertEqual(record.actor, OPS_LEAD_CODE)
        self.assertEqual(record.metadata["source"], "DIRECT")
        self.assertEqual(record.metadata["state"], "execution")

    def test_clearing_the_assignment_is_recorded_as_a_return_to_the_inherited_graph(self) -> None:
        self.assign(SHORT_FLOW)
        self.clear()

        record = self._records()[-1]
        self.assertEqual(record.from_value, SHORT_FLOW)
        self.assertEqual(record.to_value, DEFAULT_FLOW)
        self.assertEqual(record.metadata["source"], "INHERITED")

    def test_assigning_the_graph_a_project_already_follows_records_nothing(self) -> None:
        self.assign(SHORT_FLOW)
        response = self.assign(SHORT_FLOW)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(self._records()), 1)

    def test_clearing_an_assignment_that_was_never_made_records_nothing(self) -> None:
        response = self.clear()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(self._records(), [])

    def test_a_reassignment_never_claims_the_project_changed_state(self) -> None:
        self.assign(SHORT_FLOW)

        self.assertFalse(
            ActivityRecord.objects.filter(
                entity_id=PROJECT_CODE, verb=ActivityRecord.Verb.STATE_CHANGED
            ).exists()
        )


class ProjectWorkflowOutboxTestCase(ProjectWorkflowSurface, TransactionTestCase):
    """The event a reassignment publishes actually commits.

    ``TransactionTestCase`` because this is the assertion ``TestCase`` cannot make: it would show
    the ``OutboxEvent`` row inside a transaction that never commits, which is exactly what an event
    nobody will ever deliver also looks like.
    """

    def setUp(self) -> None:
        self.scenario = PortfolioScenario()
        self.scenario.add_transitions()
        self.scenario.add_ops_lead()
        _short_flow()
        self.auth = bearer(username=OPS_LEAD_CODE)

    def _events(self) -> list[OutboxEvent]:
        return list(OutboxEvent.objects.filter(entity_id=PROJECT_CODE).order_by("created_at"))

    def test_a_reassignment_goes_out_as_a_project_update_naming_both_graphs(self) -> None:
        self.assign(SHORT_FLOW)

        events = self._events()
        self.assertEqual([event.topic for event in events], ["project.updated"])
        self.assertEqual(
            events[0].payload["changes"],
            {"workflow": {"from": DEFAULT_FLOW, "to": SHORT_FLOW}},
        )

    def test_the_payload_claims_no_state_change_because_there_was_none(self) -> None:
        self.assign(SHORT_FLOW)

        self.assertNotIn("workflow_state", self._events()[0].payload["changes"])

    def test_a_refused_reassignment_publishes_nothing(self) -> None:
        self.scenario.block_project()

        self.assign(SHORT_FLOW)

        self.assertEqual(self._events(), [])
