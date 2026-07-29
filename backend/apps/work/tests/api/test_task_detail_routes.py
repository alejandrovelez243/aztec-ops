"""HTTP behaviour of the single-task read and the single-task edit.

Two things are worth proving here and the rest is plumbing. The detail must draw the whole screen
in one response — the task, the workflow's own answer about where it may go next, and the comments
written against it — because a screen assembled from two requests is a screen that renders half of
itself when the second one fails. And the edit must leave a trace: changing who carries a task is
exactly the kind of change that gets argued about a week later, so a ``PATCH`` that moved the
assignee without writing an ``ActivityRecord`` would be worse than one that failed.
"""

import json
from typing import Any

from django.test import Client, TestCase

from apps.accounts.models import User
from apps.accounts.tests.support import bearer, make_member
from apps.activity.models import ActivityRecord
from apps.portfolio.tests.scenario import OWNER_CODE, PROJECT_CODE, PortfolioScenario


class TaskDetailRouteTestCase(TestCase):
    """``GET /api/v1/tasks/{code}`` — one request draws the whole task screen."""

    scenario: PortfolioScenario
    auth: dict[str, str]
    task_code: str

    @classmethod
    def setUpTestData(cls) -> None:
        cls.scenario = PortfolioScenario()
        cls.scenario.add_task_workflow()
        cls.auth = bearer(username=OWNER_CODE)
        cls.task_code = _create_task(cls.auth)

    def test_the_detail_names_the_task_and_the_project_it_hangs_off(self) -> None:
        body = self.client.get(f"/api/v1/tasks/{self.task_code}", **self.auth).json()

        self.assertEqual(body["code"], self.task_code)
        self.assertEqual(body["project"]["code"], PROJECT_CODE)
        self.assertEqual(body["project"]["name"], "Primary test project")
        self.assertEqual(body["state"]["code"], "todo")
        self.assertEqual(body["assignee"]["alias"], OWNER_CODE)
        self.assertFalse(body["is_overdue"])

    def test_the_transitions_come_from_the_workflow_and_not_from_a_hardcoded_list(self) -> None:
        body = self.client.get(f"/api/v1/tasks/{self.task_code}", **self.auth).json()

        self.assertEqual([option["to_state"]["code"] for option in body["transitions"]], ["doing"])
        self.assertEqual(body["transitions"][0]["label"], "Empezar")

    def test_a_task_in_a_terminal_state_offers_no_transitions_rather_than_inventing_one(
        self,
    ) -> None:
        self.client.post(
            f"/api/v1/tasks/{self.task_code}/transition",
            data=json.dumps({"to_state": "doing"}),
            content_type="application/json",
            **self.auth,
        )

        body = self.client.get(f"/api/v1/tasks/{self.task_code}", **self.auth).json()

        self.assertEqual(body["state"]["code"], "doing")
        self.assertEqual(body["transitions"], [])

    def test_the_detail_carries_the_task_comments_newest_first(self) -> None:
        self._add_note("Waiting on the client's environment.")
        self._add_note("Environment granted, starting today.")

        body = self.client.get(f"/api/v1/tasks/{self.task_code}", **self.auth).json()

        self.assertEqual(
            [note["body"] for note in body["notes"]],
            ["Environment granted, starting today.", "Waiting on the client's environment."],
        )
        self.assertEqual(body["notes"][0]["author"], OWNER_CODE)

    def test_a_project_level_note_is_not_shown_as_a_comment_on_the_task(self) -> None:
        self.client.post(
            f"/api/v1/projects/{PROJECT_CODE}/notes",
            data=json.dumps({"body": "Kickoff scheduled for Monday."}),
            content_type="application/json",
            **self.auth,
        )

        body = self.client.get(f"/api/v1/tasks/{self.task_code}", **self.auth).json()

        self.assertEqual(body["notes"], [])

    def test_an_unknown_task_code_is_404_and_names_what_was_not_found(self) -> None:
        response = self.client.get("/api/v1/tasks/PRJ-T1-T99", **self.auth)

        self.assertEqual(response.status_code, 404)
        body = response.json()
        self.assertEqual(body["code"], "not_found")
        self.assertEqual(body["details"]["id"], "PRJ-T1-T99")

    def test_an_unauthenticated_read_is_401_like_every_other_route(self) -> None:
        response = self.client.get(f"/api/v1/tasks/{self.task_code}")

        self.assertEqual(response.status_code, 401)

    def _add_note(self, body: str) -> None:
        self.client.post(
            f"/api/v1/projects/{PROJECT_CODE}/notes",
            data=json.dumps({"body": body, "task_code": self.task_code}),
            content_type="application/json",
            **self.auth,
        )


class TaskUpdateRouteTestCase(TestCase):
    """``PATCH /api/v1/tasks/{code}`` — absent means untouched, and the change is recorded."""

    scenario: PortfolioScenario
    auth: dict[str, str]
    other: User
    task_code: str

    @classmethod
    def setUpTestData(cls) -> None:
        cls.scenario = PortfolioScenario()
        cls.scenario.add_task_workflow()
        cls.auth = bearer(username=OWNER_CODE)
        cls.other = make_member(code="otra.persona")
        cls.task_code = _create_task(cls.auth)

    def _patch(self, **body: Any) -> Any:
        return self.client.patch(
            f"/api/v1/tasks/{self.task_code}",
            data=json.dumps(body),
            content_type="application/json",
            **self.auth,
        )

    def test_changing_the_responsable_returns_the_task_carrying_the_new_owner(self) -> None:
        response = self._patch(assignee="otra.persona")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["assignee"]["alias"], "otra.persona")

    def test_changing_the_responsable_writes_one_activity_record_naming_both_sides(self) -> None:
        self._patch(assignee="otra.persona")

        records = ActivityRecord.objects.filter(
            entity_type="task", entity_id=self.task_code, verb="OWNER_CHANGED"
        )
        self.assertEqual(records.count(), 1)
        record = records.get()
        self.assertEqual(record.from_value, OWNER_CODE)
        self.assertEqual(record.to_value, "otra.persona")

    def test_an_explicit_null_assignee_unassigns_the_task(self) -> None:
        response = self._patch(assignee=None)

        self.assertIsNone(response.json()["assignee"])

    def test_a_field_left_out_of_the_payload_is_left_untouched(self) -> None:
        self._patch(title="Validate the release checklist twice")

        body = self.client.get(f"/api/v1/tasks/{self.task_code}", **self.auth).json()
        self.assertEqual(body["title"], "Validate the release checklist twice")
        self.assertEqual(body["assignee"]["alias"], OWNER_CODE)

    def test_an_unknown_responsable_is_refused_with_404_and_changes_nothing(self) -> None:
        response = self._patch(assignee="nadie.existe")

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["code"], "not_found")
        detail = self.client.get(f"/api/v1/tasks/{self.task_code}", **self.auth).json()
        self.assertEqual(detail["assignee"]["alias"], OWNER_CODE)

    def test_an_unknown_task_is_404(self) -> None:
        response = self.client.patch(
            "/api/v1/tasks/PRJ-T1-T99",
            data=json.dumps({"assignee": "otra.persona"}),
            content_type="application/json",
            **self.auth,
        )

        self.assertEqual(response.status_code, 404)

    def test_the_workflow_state_is_not_a_field_of_this_payload(self) -> None:
        self._patch(workflow_state="doing")

        body = self.client.get(f"/api/v1/tasks/{self.task_code}", **self.auth).json()
        self.assertEqual(body["state"]["code"], "todo")

    def test_an_unauthenticated_edit_is_401_like_every_other_route(self) -> None:
        response = self.client.patch(
            f"/api/v1/tasks/{self.task_code}",
            data=json.dumps({"assignee": "otra.persona"}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 401)


def _create_task(auth: dict[str, str]) -> str:
    """Create the one task these classes read and edit, through the route that allocates its code.

    Built over HTTP rather than with ``Task.objects.create`` so it lands in the ``is_initial`` state
    of the *task* workflow. A task inserted directly would carry whatever state the fixture picked,
    and its ``transitions`` would then prove nothing about where the workflow says it may go.

    Uses its own :class:`~django.test.Client` because ``setUpTestData`` runs before ``self.client``
    exists.
    """
    response = Client().post(
        f"/api/v1/projects/{PROJECT_CODE}/tasks",
        data=json.dumps(
            {
                "title": "Validate release checklist",
                "priority": "critica",
                "assignee": OWNER_CODE,
            }
        ),
        content_type="application/json",
        **auth,
    )
    return str(response.json()["code"])
