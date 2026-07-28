"""HTTP behaviour of the task, blocker and note routes.

The point of these is the same as the project routes': a rejection the domain considers ordinary
must arrive as its documented status, not as a 500. Resolving a blocker twice is the sharpest case
— it is legal in the workflow and impossible against the facts, which is exactly what
``409 conflicting_state`` means.
"""

import json
from typing import Any

from django.test import TestCase

from apps.portfolio.tests.scenario import OWNER_CODE, PROJECT_CODE, PortfolioScenario

ACTOR = {"HTTP_X_ACTOR": OWNER_CODE}


class TaskRouteTestCase(TestCase):
    scenario: PortfolioScenario

    @classmethod
    def setUpTestData(cls) -> None:
        cls.scenario = PortfolioScenario()
        cls.scenario.add_task_workflow()

    def _create_task(self, **body: Any) -> Any:
        return self.client.post(
            f"/api/v1/projects/{PROJECT_CODE}/tasks",
            data=json.dumps({"title": "Validate release checklist", "priority": "critica", **body}),
            content_type="application/json",
            **ACTOR,
        )

    def test_creating_a_task_allocates_a_project_scoped_code_and_the_initial_state(self) -> None:
        response = self._create_task()

        self.assertEqual(response.status_code, 201)
        body = response.json()
        self.assertEqual(body["code"], f"{PROJECT_CODE}-T01")
        self.assertEqual(body["state"]["code"], "todo")

    def test_a_second_task_continues_the_series_rather_than_reusing_the_first_code(self) -> None:
        self._create_task()

        self.assertEqual(self._create_task().json()["code"], f"{PROJECT_CODE}-T02")

    def test_an_unknown_priority_is_404_because_the_taxonomy_row_does_not_exist(self) -> None:
        response = self._create_task(priority="urgentisima")

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["code"], "not_found")

    def test_listing_tasks_of_an_unknown_project_is_404_not_an_empty_page(self) -> None:
        response = self.client.get("/api/v1/projects/PRJ-NOPE/tasks")

        self.assertEqual(response.status_code, 404)

    def test_the_task_list_is_paginated_with_the_total_match_count(self) -> None:
        self._create_task()
        self._create_task()

        body = self.client.get(f"/api/v1/projects/{PROJECT_CODE}/tasks?page_size=1").json()

        self.assertEqual(len(body["items"]), 1)
        self.assertEqual(body["count"], 2)

    def test_an_order_by_outside_the_allowlist_is_422_and_names_what_is_allowed(self) -> None:
        response = self.client.get(f"/api/v1/projects/{PROJECT_CODE}/tasks?order_by=secret_column")

        self.assertEqual(response.status_code, 422)
        body = response.json()
        self.assertEqual(body["code"], "validation_error")
        self.assertIn("due_date", body["details"]["allowed"])

    def test_a_declared_task_move_returns_the_task_in_its_new_state(self) -> None:
        code = self._create_task().json()["code"]

        response = self.client.post(
            f"/api/v1/tasks/{code}/transition",
            data=json.dumps({"to_state": "doing"}),
            content_type="application/json",
            **ACTOR,
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["state"]["code"], "doing")

    def test_an_undeclared_task_move_is_409_and_not_a_500(self) -> None:
        code = self._create_task().json()["code"]

        response = self.client.post(
            f"/api/v1/tasks/{code}/transition",
            data=json.dumps({"to_state": "todo"}),
            content_type="application/json",
            **ACTOR,
        )

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["code"], "transition_not_allowed")


class BlockerRouteTestCase(TestCase):
    scenario: PortfolioScenario

    @classmethod
    def setUpTestData(cls) -> None:
        cls.scenario = PortfolioScenario()

    def _raise_blocker(self) -> Any:
        return self.client.post(
            f"/api/v1/projects/{PROJECT_CODE}/blockers",
            data=json.dumps({"kind": "ACCESS", "description": "Repository access pending."}),
            content_type="application/json",
            **ACTOR,
        )

    def test_raising_a_blocker_returns_it_open_with_a_zero_age(self) -> None:
        response = self._raise_blocker()

        self.assertEqual(response.status_code, 201)
        body = response.json()
        self.assertIsNone(body["resolved_at"])
        self.assertEqual(body["age_days"], 0)

    def test_raising_a_blocker_does_not_move_the_project_state(self) -> None:
        self._raise_blocker()

        detail = self.client.get(f"/api/v1/projects/{PROJECT_CODE}").json()
        self.assertEqual(detail["state"]["code"], "execution")
        self.assertEqual(detail["open_blockers"], 1)

    def test_resolving_a_blocker_records_when_and_how(self) -> None:
        blocker_id = self._raise_blocker().json()["id"]

        response = self.client.post(
            f"/api/v1/blockers/{blocker_id}/resolve",
            data=json.dumps({"resolution": "Client granted access."}),
            content_type="application/json",
            **ACTOR,
        )

        self.assertEqual(response.status_code, 200)
        self.assertIsNotNone(response.json()["resolved_at"])

    def test_resolving_twice_is_the_documented_conflict_not_a_silent_overwrite(self) -> None:
        blocker_id = self._raise_blocker().json()["id"]
        payload = json.dumps({"resolution": "Client granted access."})
        self.client.post(
            f"/api/v1/blockers/{blocker_id}/resolve",
            data=payload,
            content_type="application/json",
            **ACTOR,
        )

        response = self.client.post(
            f"/api/v1/blockers/{blocker_id}/resolve",
            data=json.dumps({"resolution": "Again."}),
            content_type="application/json",
            **ACTOR,
        )

        self.assertEqual(response.status_code, 409)
        body = response.json()
        self.assertEqual(body["code"], "conflicting_state")
        self.assertEqual(body["details"]["current"], "resolved")

    def test_an_unknown_blocker_is_404(self) -> None:
        response = self.client.post(
            "/api/v1/blockers/999999/resolve",
            data=json.dumps({"resolution": "Nothing to resolve."}),
            content_type="application/json",
            **ACTOR,
        )

        self.assertEqual(response.status_code, 404)


class NoteRouteTestCase(TestCase):
    scenario: PortfolioScenario

    @classmethod
    def setUpTestData(cls) -> None:
        cls.scenario = PortfolioScenario()

    def test_a_note_records_its_author_from_the_actor_header(self) -> None:
        response = self.client.post(
            f"/api/v1/projects/{PROJECT_CODE}/notes",
            data=json.dumps({"body": "Kickoff scheduled for Monday."}),
            content_type="application/json",
            **ACTOR,
        )

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()["author"], OWNER_CODE)

    def test_an_empty_note_body_is_rejected_by_the_schema_in_the_same_envelope(self) -> None:
        response = self.client.post(
            f"/api/v1/projects/{PROJECT_CODE}/notes",
            data=json.dumps({"body": ""}),
            content_type="application/json",
            **ACTOR,
        )

        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["code"], "validation_error")
