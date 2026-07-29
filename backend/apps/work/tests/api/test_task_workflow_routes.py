"""HTTP behaviour of putting one task on a lifecycle of its own.

The task counterpart of ``apps.portfolio.tests.api.test_project_workflow_routes``, and the same
three things are what matter: the reassignment keeps the state the task was standing on, the legal
moves afterwards come from the **new** graph, and a task standing on a state the target does not
contain is refused rather than parked outside its own lifecycle.

The extra fact worth proving on this side is that the assignment is per *task*: two tasks of one
project can follow different lifecycles, which is the point of putting the column on the record
rather than on the engagement type everything shares.

``TestCase``: the outbox assertions for this write live where they can commit, and the shape of the
``task.updated`` payload is identical to the project one already covered.
"""

import json
from typing import Any

from django.test import Client, TestCase

from apps.accounts.tests.support import bearer, make_member
from apps.activity.models import ActivityRecord
from apps.portfolio.tests.scenario import (
    OPS_LEAD_CODE,
    OWNER_CODE,
    PROJECT_CODE,
    PortfolioScenario,
)
from apps.work.models import Task
from apps.workflow.models import (
    AppliesTo,
    StateCategory,
    Workflow,
    WorkflowState,
    WorkflowTransition,
)

#: The lifecycle tasks are reassigned onto. It shares ``todo`` with the scenario's task graph, which
#: is what makes a clean landing possible, and has no ``doing``, which is what makes the
#: incompatible case reachable.
REVIEW_FLOW = "ciclo-revision"

#: The scenario's own task graph, the one every task inherits.
DEFAULT_TASK_FLOW = "test-task-flow"

#: A signed-in member who is not an ops lead.
MEMBER_CODE = "diego.rojas"


def _review_flow() -> Workflow:
    """Build the second task lifecycle: ``todo`` → ``en_revision``, and nothing else."""
    workflow = Workflow.objects.create(
        code=REVIEW_FLOW,
        name="Ciclo de revisión",
        applies_to=AppliesTo.TASK,
    )
    todo = WorkflowState.objects.create(
        workflow=workflow,
        code="todo",
        label="Por hacer",
        category=StateCategory.BACKLOG,
        is_initial=True,
        order=0,
    )
    review = WorkflowState.objects.create(
        workflow=workflow,
        code="en_revision",
        label="En revisión",
        category=StateCategory.IN_PROGRESS,
        order=1,
    )
    WorkflowTransition.objects.create(
        workflow=workflow,
        from_state=todo,
        to_state=review,
        label="Mandar a revisión",
        order=0,
    )
    return workflow


def _create_task(auth: dict[str, str], *, title: str) -> str:
    """Create one task through the route that allocates its code and its initial state.

    Over HTTP rather than with ``Task.objects.create`` so the task lands on the ``is_initial`` node
    of the *task* workflow, which is what makes its ``transitions`` mean anything. Uses its own
    client because ``setUpTestData`` runs before ``self.client`` exists.
    """
    response = Client().post(
        f"/api/v1/projects/{PROJECT_CODE}/tasks",
        data=json.dumps({"title": title, "priority": "critica", "assignee": OWNER_CODE}),
        content_type="application/json",
        **auth,
    )
    return str(response.json()["code"])


class TaskWorkflowSurface:
    """The two writes and the read, as one signed-in client issues them."""

    client: Any
    auth: dict[str, str]
    task_code: str

    def assign(self, workflow: str, *, task: str | None = None) -> Any:
        return self.client.put(
            f"/api/v1/tasks/{task or self.task_code}/workflow",
            data=json.dumps({"workflow": workflow}),
            content_type="application/json",
            **self.auth,
        )

    def clear(self, *, task: str | None = None) -> Any:
        return self.client.delete(f"/api/v1/tasks/{task or self.task_code}/workflow", **self.auth)

    def detail(self, *, task: str | None = None) -> Any:
        return self.client.get(f"/api/v1/tasks/{task or self.task_code}", **self.auth).json()


class TaskFollowsItsOwnWorkflowTestCase(TaskWorkflowSurface, TestCase):
    """A reassignment that lands cleanly, and what the task obeys afterwards."""

    scenario: PortfolioScenario
    auth: dict[str, str]
    task_code: str
    sibling_code: str

    @classmethod
    def setUpTestData(cls) -> None:
        cls.scenario = PortfolioScenario()
        cls.scenario.add_task_workflow()
        cls.scenario.add_ops_lead()
        _review_flow()
        cls.auth = bearer(username=OPS_LEAD_CODE)
        cls.task_code = _create_task(cls.auth, title="Revisar el checklist")
        cls.sibling_code = _create_task(cls.auth, title="Preparar el entorno")

    def test_assigning_a_lifecycle_keeps_the_state_the_task_was_standing_on(self) -> None:
        response = self.assign(REVIEW_FLOW)

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["workflow"]["code"], REVIEW_FLOW)
        self.assertEqual(body["workflow"]["source"], "DIRECT")
        self.assertEqual(body["state"]["code"], "todo")

    def test_the_legal_moves_after_a_reassignment_come_from_the_new_graph(self) -> None:
        before = [option["to_state"]["code"] for option in self.detail()["transitions"]]

        self.assign(REVIEW_FLOW)
        after = [option["to_state"]["code"] for option in self.detail()["transitions"]]

        self.assertEqual(before, ["doing"])
        self.assertEqual(after, ["en_revision"])

    def test_a_move_the_old_graph_offered_is_refused_once_the_new_one_governs(self) -> None:
        self.assign(REVIEW_FLOW)

        response = self.client.post(
            f"/api/v1/tasks/{self.task_code}/transition",
            data=json.dumps({"to_state": "doing"}),
            content_type="application/json",
            **self.auth,
        )

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["details"]["allowed"], ["en_revision"])

    def test_two_tasks_of_one_project_may_follow_different_lifecycles(self) -> None:
        self.assign(REVIEW_FLOW)

        sibling = self.detail(task=self.sibling_code)
        self.assertEqual(sibling["workflow"]["code"], DEFAULT_TASK_FLOW)
        self.assertEqual(sibling["workflow"]["source"], "INHERITED")
        self.assertEqual(
            [option["to_state"]["code"] for option in sibling["transitions"]], ["doing"]
        )

    def test_clearing_the_assignment_hands_the_task_back_to_its_binding(self) -> None:
        self.assign(REVIEW_FLOW)

        body = self.clear().json()

        self.assertEqual(body["workflow"]["code"], DEFAULT_TASK_FLOW)
        self.assertEqual(body["workflow"]["source"], "INHERITED")

    def test_the_column_is_written_and_agrees_with_the_state_the_task_stands_on(self) -> None:
        self.assign(REVIEW_FLOW)

        task = Task.objects.get(code=self.task_code)
        self.assertEqual(task.workflow.code, REVIEW_FLOW)
        self.assertEqual(task.workflow_state.workflow_id, task.workflow_id)

    def test_the_trail_names_both_graphs_and_the_project_the_task_belongs_to(self) -> None:
        self.assign(REVIEW_FLOW)

        record = ActivityRecord.objects.get(
            entity_id=self.task_code, verb=ActivityRecord.Verb.WORKFLOW_ASSIGNED
        )
        self.assertEqual(record.from_value, DEFAULT_TASK_FLOW)
        self.assertEqual(record.to_value, REVIEW_FLOW)
        self.assertEqual(record.metadata["project_code"], PROJECT_CODE)
        self.assertEqual(record.metadata["source"], "DIRECT")


class InheritedTaskWorkflowRegressionTestCase(TaskWorkflowSurface, TestCase):
    """A task nobody assigned resolves exactly as it did before this feature existed."""

    scenario: PortfolioScenario
    auth: dict[str, str]
    task_code: str

    @classmethod
    def setUpTestData(cls) -> None:
        cls.scenario = PortfolioScenario()
        cls.scenario.add_task_workflow()
        cls.auth = bearer(username=OWNER_CODE)
        cls.task_code = _create_task(cls.auth, title="Revisar el checklist")

    def test_an_untouched_task_follows_the_graph_its_binding_gives_it(self) -> None:
        self.assertEqual(
            self.detail()["workflow"],
            {"code": DEFAULT_TASK_FLOW, "name": "Test task flow", "source": "INHERITED"},
        )

    def test_an_untouched_task_stores_no_assignment_at_all(self) -> None:
        self.assertIsNone(Task.objects.get(code=self.task_code).workflow_id)

    def test_a_newly_created_task_still_starts_on_the_bound_graph_entry_node(self) -> None:
        self.assertEqual(self.detail()["state"]["code"], "todo")


class IncompatibleTaskReassignmentTestCase(TaskWorkflowSurface, TestCase):
    """The hard case, task side: the target has no column for where the task is standing."""

    scenario: PortfolioScenario
    auth: dict[str, str]
    task_code: str

    @classmethod
    def setUpTestData(cls) -> None:
        cls.scenario = PortfolioScenario()
        cls.scenario.add_task_workflow()
        cls.scenario.add_ops_lead()
        _review_flow()
        cls.auth = bearer(username=OPS_LEAD_CODE)
        cls.task_code = _create_task(cls.auth, title="Revisar el checklist")

    def setUp(self) -> None:
        # ``doing`` exists only in the scenario's task graph, so a task moved there has no
        # equivalent node in the review cycle.
        self.client.post(
            f"/api/v1/tasks/{self.task_code}/transition",
            data=json.dumps({"to_state": "doing"}),
            content_type="application/json",
            **self.auth,
        )

    def test_a_task_on_a_state_the_target_lacks_is_refused(self) -> None:
        response = self.assign(REVIEW_FLOW)

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["code"], "conflicting_state")

    def test_the_refusal_names_the_state_the_record_and_what_the_target_offers(self) -> None:
        details = self.assign(REVIEW_FLOW).json()["details"]

        self.assertEqual(details["id"], "doing")
        self.assertEqual(details["record"], self.task_code)
        self.assertEqual(details["workflow"], REVIEW_FLOW)
        self.assertEqual(details["available"], ["todo", "en_revision"])

    def test_a_refused_reassignment_changes_nothing_at_all(self) -> None:
        self.assign(REVIEW_FLOW)

        task = Task.objects.get(code=self.task_code)
        self.assertIsNone(task.workflow_id)
        self.assertEqual(task.workflow_state.code, "doing")


class TaskReassignmentRefusalTestCase(TaskWorkflowSurface, TestCase):
    """Addressing something that is not there, or a graph that governs something else."""

    scenario: PortfolioScenario
    auth: dict[str, str]
    task_code: str

    @classmethod
    def setUpTestData(cls) -> None:
        cls.scenario = PortfolioScenario()
        cls.scenario.add_task_workflow()
        cls.scenario.add_ops_lead()
        _review_flow()
        cls.auth = bearer(username=OPS_LEAD_CODE)
        cls.task_code = _create_task(cls.auth, title="Revisar el checklist")

    def test_an_unknown_workflow_is_a_404_naming_it(self) -> None:
        response = self.assign("no-existe")

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["details"]["id"], "no-existe")

    def test_an_unknown_task_is_a_404_naming_the_task(self) -> None:
        response = self.assign(REVIEW_FLOW, task="PRJ-T1-T99")

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["details"], {"entity": "task", "id": "PRJ-T1-T99"})

    def test_a_project_lifecycle_is_refused_for_a_task_naming_the_field(self) -> None:
        response = self.assign("test-project-flow")

        self.assertEqual(response.status_code, 422)
        self.assertIn("workflow", response.json()["details"]["fields"])


class TaskReassignmentPermissionTestCase(TaskWorkflowSurface, TestCase):
    """Reading which lifecycle a task follows is everybody's; deciding it is an ops lead's."""

    scenario: PortfolioScenario
    auth: dict[str, str]
    task_code: str

    @classmethod
    def setUpTestData(cls) -> None:
        cls.scenario = PortfolioScenario()
        cls.scenario.add_task_workflow()
        make_member(code=MEMBER_CODE)
        _review_flow()
        cls.task_code = _create_task(bearer(username=OWNER_CODE), title="Revisar el checklist")
        cls.auth = bearer(username=MEMBER_CODE)

    def test_a_member_who_is_not_an_ops_lead_may_not_assign_a_lifecycle(self) -> None:
        response = self.assign(REVIEW_FLOW)

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["details"]["required"], "ops_lead")

    def test_a_member_who_is_not_an_ops_lead_may_not_clear_one_either(self) -> None:
        self.assertEqual(self.clear().status_code, 403)

    def test_that_member_may_still_read_which_lifecycle_the_task_follows(self) -> None:
        self.assertEqual(self.detail()["workflow"]["code"], DEFAULT_TASK_FLOW)
