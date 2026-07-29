"""Removing a task, and what "removed" is allowed to mean (ADR 0012).

A soft delete is only worth the column if it is invisible in every read that counts and permanent
in every read that identifies. Both halves are proved here, and the second half is the one that
rots quietly: a removed task that stays out of the list but whose code gets reused, or whose
dependency edge stops constraining the graph, is a delete that lies about being reversible.

The outbox assertions live on ``TransactionTestCase`` on purpose. ``TestCase`` wraps every test in
a transaction that never commits, so an ``OutboxEvent`` asserted there looks identical to an event
nobody will ever deliver (CLAUDE.md rule 15).
"""

import json
from typing import Any

from django.test import Client, TestCase, TransactionTestCase

from apps.accounts.tests.support import bearer
from apps.activity.models import ActivityRecord
from apps.events.models import OutboxEvent
from apps.events.tests.celery_support import EagerCeleryMixin
from apps.events.tests.registry_support import only_handlers
from apps.portfolio.tests.scenario import OWNER_CODE, PROJECT_CODE, PortfolioScenario
from apps.work.domain.dependencies import find_dependency_cycle
from apps.work.models import Task, TaskDependency

TASKS_URL = f"/api/v1/projects/{PROJECT_CODE}/tasks"


class TaskRemovalRouteTestCase(TestCase):
    """``DELETE /api/v1/tasks/{code}`` — 204, reversible, and idempotent."""

    scenario: PortfolioScenario
    auth: dict[str, str]
    task_code: str

    @classmethod
    def setUpTestData(cls) -> None:
        cls.scenario = PortfolioScenario()
        cls.scenario.add_task_workflow()
        cls.auth = bearer(username=OWNER_CODE)
        cls.task_code = _create_task(cls.auth, title="Validate release checklist")

    def test_removing_a_task_answers_204_and_flags_the_row_instead_of_deleting_it(self) -> None:
        response = self.client.delete(f"/api/v1/tasks/{self.task_code}", **self.auth)

        self.assertEqual(response.status_code, 204)
        task = Task.objects.get(code=self.task_code)
        self.assertTrue(task.is_archived)

    def test_removing_a_task_records_it_against_the_project_timeline(self) -> None:
        self.client.delete(f"/api/v1/tasks/{self.task_code}", **self.auth)

        record = ActivityRecord.objects.get(verb="TASK_REMOVED")
        # Against the project, like ``TASK_ADDED``: a record filed under a task nobody can open
        # any more is a record nobody reads.
        self.assertEqual(record.entity_type, "project")
        self.assertEqual(record.entity_id, PROJECT_CODE)
        # ``from_value`` where ``TASK_ADDED`` uses ``to_value``: removed *from* the project.
        self.assertEqual(record.from_value, self.task_code)
        self.assertEqual(record.to_value, "")
        self.assertEqual(record.metadata["task_code"], self.task_code)

    def test_removing_an_already_removed_task_is_the_same_204_and_writes_nothing_twice(
        self,
    ) -> None:
        self.client.delete(f"/api/v1/tasks/{self.task_code}", **self.auth)

        response = self.client.delete(f"/api/v1/tasks/{self.task_code}", **self.auth)

        self.assertEqual(response.status_code, 204)
        self.assertEqual(ActivityRecord.objects.filter(verb="TASK_REMOVED").count(), 1)

    def test_removing_a_code_that_names_no_task_is_404(self) -> None:
        response = self.client.delete("/api/v1/tasks/PRJ-T1-T99", **self.auth)

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["code"], "not_found")

    def test_an_unauthenticated_removal_is_401_like_every_other_write(self) -> None:
        response = self.client.delete(f"/api/v1/tasks/{self.task_code}")

        self.assertEqual(response.status_code, 401)

    def test_a_patch_restores_a_removed_task_and_returns_it_rather_than_404ing(self) -> None:
        self.client.delete(f"/api/v1/tasks/{self.task_code}", **self.auth)

        response = self._patch(is_archived=False)

        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["is_archived"])
        self.assertEqual(
            self.client.get(f"/api/v1/tasks/{self.task_code}", **self.auth).status_code, 200
        )

    def test_restoring_records_the_task_returning_to_the_project(self) -> None:
        self.client.delete(f"/api/v1/tasks/{self.task_code}", **self.auth)

        self._patch(is_archived=False)

        record = ActivityRecord.objects.get(verb="TASK_RESTORED")
        self.assertEqual(record.entity_type, "project")
        self.assertEqual(record.entity_id, PROJECT_CODE)
        self.assertEqual(record.to_value, self.task_code)
        self.assertEqual(record.from_value, "")

    def test_restoring_a_task_that_was_never_removed_records_nothing(self) -> None:
        self._patch(is_archived=False)

        self.assertFalse(ActivityRecord.objects.filter(verb="TASK_RESTORED").exists())

    def _patch(self, **body: Any) -> Any:
        return self.client.patch(
            f"/api/v1/tasks/{self.task_code}",
            data=json.dumps(body),
            content_type="application/json",
            **self.auth,
        )


class RemovedTaskVisibilityTestCase(TestCase):
    """Where a removed task disappears from, and where it deliberately does not.

    One class rather than one per surface, because the value of this matrix is that it is read
    together: every row of it is the same removal, and the three exemptions at the bottom only
    make sense next to the seven absences above them.
    """

    scenario: PortfolioScenario
    auth: dict[str, str]
    kept_code: str
    removed_code: str

    def setUp(self) -> None:
        self.scenario = PortfolioScenario()
        self.scenario.add_task_workflow()
        self.auth = bearer(username=OWNER_CODE)
        self.kept_code = _create_task(self.auth, title="Task that stays")
        self.removed_code = _create_task(self.auth, title="Task that goes")
        self.client.delete(f"/api/v1/tasks/{self.removed_code}", **self.auth)

    def test_it_is_absent_from_the_task_list_and_from_its_count(self) -> None:
        body = self.client.get(TASKS_URL, **self.auth).json()

        self.assertEqual([task["code"] for task in body["items"]], [self.kept_code])
        self.assertEqual(body["count"], 1)

    def test_asking_for_the_removed_ones_returns_it_and_only_it(self) -> None:
        body = self.client.get(f"{TASKS_URL}?is_archived=true", **self.auth).json()

        self.assertEqual([task["code"] for task in body["items"]], [self.removed_code])
        self.assertTrue(body["items"][0]["is_archived"])

    def test_it_is_absent_from_the_project_detail_and_from_its_three_counts(self) -> None:
        body = self.client.get(f"/api/v1/projects/{PROJECT_CODE}", **self.auth).json()

        self.assertEqual([task["code"] for task in body["tasks"]], [self.kept_code])
        self.assertEqual(body["open_tasks"], 1)
        self.assertEqual(body["overdue_tasks"], 0)
        self.assertEqual(body["blocked_tasks"], 0)

    def test_reading_it_by_code_is_404(self) -> None:
        response = self.client.get(f"/api/v1/tasks/{self.removed_code}", **self.auth)

        self.assertEqual(response.status_code, 404)

    def test_moving_it_along_a_workflow_edge_is_404(self) -> None:
        response = self.client.post(
            f"/api/v1/tasks/{self.removed_code}/transition",
            data=json.dumps({"to_state": "doing"}),
            content_type="application/json",
            **self.auth,
        )

        self.assertEqual(response.status_code, 404)

    def test_it_is_refused_as_the_target_of_a_note(self) -> None:
        response = self.client.post(
            f"/api/v1/projects/{PROJECT_CODE}/notes",
            data=json.dumps({"body": "Still pending?", "task_code": self.removed_code}),
            content_type="application/json",
            **self.auth,
        )

        self.assertEqual(response.status_code, 404)

    def test_it_is_refused_as_the_target_of_a_blocker(self) -> None:
        response = self.client.post(
            f"/api/v1/projects/{PROJECT_CODE}/blockers",
            data=json.dumps(
                {
                    "kind": "EXTERNAL_DEPENDENCY",
                    "description": "Waiting on the vendor.",
                    "task_code": self.removed_code,
                }
            ),
            content_type="application/json",
            **self.auth,
        )

        self.assertEqual(response.status_code, 404)

    def test_it_is_refused_as_a_prerequisite_of_a_new_task(self) -> None:
        response = self.client.post(
            TASKS_URL,
            data=json.dumps(
                {
                    "title": "Depends on removed work",
                    "priority": "critica",
                    "depends_on": [self.removed_code],
                }
            ),
            content_type="application/json",
            **self.auth,
        )

        self.assertEqual(response.status_code, 404)

    def test_its_code_is_never_handed_to_another_task(self) -> None:
        # The exemption that matters most: a reused code would silently merge two histories in
        # the timeline and make an already-published ``task.created`` name the wrong task.
        response = self.client.post(
            TASKS_URL,
            data=json.dumps({"title": "The next one", "priority": "critica"}),
            content_type="application/json",
            **self.auth,
        )

        self.assertNotEqual(response.json()["code"], self.removed_code)
        self.assertEqual(Task.objects.filter(code=self.removed_code).count(), 1)

    def test_an_existing_edge_on_it_still_constrains_the_graph(self) -> None:
        # The graph keeps its removed nodes, so the cycle check cannot be tricked by
        # archive-then-restore: remove the task that closes a loop, add the edge the loop forbade,
        # restore the task, and the invariant would be gone. Asserted against the adjacency the
        # pure check consumes, because that mapping *is* the graph as far as the rule is concerned.
        removed = Task.objects.get(code=self.removed_code)
        kept = Task.objects.get(code=self.kept_code)
        TaskDependency.objects.create(task=removed, depends_on=kept, is_resolved=True)

        adjacency = TaskDependency.objects.for_project(removed.project_id).resolved().adjacency()

        self.assertEqual(adjacency[self.removed_code], (self.kept_code,))
        self.assertIsNotNone(
            find_dependency_cycle(
                graph=adjacency, task_code=self.kept_code, depends_on_code=self.removed_code
            )
        )

    def test_a_blocker_raised_before_the_removal_stays_open_on_the_project(self) -> None:
        # Hiding it would let an operator clear a project's BLOCKED flag by removing a task,
        # which is exactly the accounting the blocker table exists to prevent.
        kept = _create_task(self.auth, title="Task about to be blocked")
        self.client.post(
            f"/api/v1/projects/{PROJECT_CODE}/blockers",
            data=json.dumps(
                {"kind": "EXTERNAL_DEPENDENCY", "description": "Vendor silent.", "task_code": kept}
            ),
            content_type="application/json",
            **self.auth,
        )

        self.client.delete(f"/api/v1/tasks/{kept}", **self.auth)

        body = self.client.get(f"/api/v1/projects/{PROJECT_CODE}", **self.auth).json()
        self.assertEqual(body["open_blockers"], 1)
        self.assertEqual([blocker["task_code"] for blocker in body["blockers"]], [kept])

    def test_a_removed_task_stops_counting_toward_its_owners_load(self) -> None:
        # Owner load is what ``OWNER_OVERLOADED`` is computed from, so counting removed work would
        # keep somebody flagged as overloaded for a backlog that no longer exists.
        body = self.client.get("/api/v1/team/load", **self.auth).json()

        loads = {row["alias"]: row for row in body["items"]}
        self.assertEqual(loads[OWNER_CODE]["open_tasks"], 1)


class TaskRemovalOutboxTestCase(EagerCeleryMixin, TransactionTestCase):
    """The removal actually commits an event, and exactly one topic carries both directions.

    ``TransactionTestCase`` because this is the assertion ``TestCase`` cannot make: it would show
    the ``OutboxEvent`` row inside a transaction that never commits, which is what an event nobody
    will ever deliver also looks like.

    ``only_handlers()`` with no handlers registered, so the drain the commit kicks off has nothing
    to run: this test is about what the producer wrote, not about what the engine does with it.
    """

    def setUp(self) -> None:
        super().setUp()
        self.scenario = PortfolioScenario()
        self.scenario.add_task_workflow()
        self.auth = bearer(username=OWNER_CODE)
        self.task_code = _create_task(self.auth, title="Validate release checklist")

    def _topics_for(self, entity_id: str) -> list[str]:
        # Ordered by ``created_at`` and never by ``id``: the primary key is a UUID minted before
        # the insert, so ordering on it would shuffle the events into an order nothing produced.
        return list(
            OutboxEvent.objects.filter(entity_id=entity_id)
            .order_by("created_at")
            .values_list("topic", flat=True)
        )

    def test_removing_a_task_commits_one_archive_changed_event(self) -> None:
        with only_handlers():
            self.client.delete(f"/api/v1/tasks/{self.task_code}", **self.auth)

        self.assertEqual(self._topics_for(self.task_code), ["task.created", "task.archive_changed"])
        event = OutboxEvent.objects.get(topic="task.archive_changed")
        self.assertEqual(event.entity_type, "task")
        self.assertTrue(event.payload["is_archived"])
        self.assertEqual(event.payload["project_code"], PROJECT_CODE)

    def test_restoring_reuses_the_same_topic_with_the_boolean_flipped(self) -> None:
        with only_handlers():
            self.client.delete(f"/api/v1/tasks/{self.task_code}", **self.auth)
            self.client.patch(
                f"/api/v1/tasks/{self.task_code}",
                data=json.dumps({"is_archived": False}),
                content_type="application/json",
                **self.auth,
            )

        # One topic and not a ``task.deleted``/``task.restored`` pair: a subscriber's question is
        # a boolean, and two topics would make every subscription list both.
        self.assertEqual(
            self._topics_for(self.task_code),
            ["task.created", "task.archive_changed", "task.archive_changed"],
        )
        latest = OutboxEvent.objects.filter(topic="task.archive_changed").order_by("created_at")
        self.assertFalse(list(latest)[-1].payload["is_archived"])

    def test_a_second_removal_commits_no_second_event(self) -> None:
        with only_handlers():
            self.client.delete(f"/api/v1/tasks/{self.task_code}", **self.auth)
            self.client.delete(f"/api/v1/tasks/{self.task_code}", **self.auth)

        self.assertEqual(
            OutboxEvent.objects.filter(topic="task.archive_changed").count(),
            1,
        )

    def test_removal_and_an_attribute_edit_in_one_request_emit_two_distinct_facts(self) -> None:
        with only_handlers():
            self.client.patch(
                f"/api/v1/tasks/{self.task_code}",
                data=json.dumps({"title": "Renamed on the way out", "is_archived": True}),
                content_type="application/json",
                **self.auth,
            )

        self.assertEqual(
            self._topics_for(self.task_code),
            ["task.created", "task.archive_changed", "task.updated"],
        )


def _create_task(auth: dict[str, str], *, title: str) -> str:
    """Create one task through the route that allocates its code.

    Built over HTTP rather than with ``Task.objects.create`` so it lands in the ``is_initial``
    state of the *task* workflow, and so the code it gets is the one ``next_code_for`` minted —
    which is the thing the reuse assertion is about.
    """
    response = Client().post(
        TASKS_URL,
        data=json.dumps({"title": title, "priority": "critica", "assignee": OWNER_CODE}),
        content_type="application/json",
        **auth,
    )
    return str(response.json()["code"])
