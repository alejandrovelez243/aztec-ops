"""Editing what a task waits on, after it was created.

``depends_on`` used to be write-once: it existed on the creation body and nowhere else, so an
operator who learned on Tuesday what a task was blocked by had to delete it and make it again.
What is proved here is the shape that replaced that — the whole set is sent, the list *is* the new
set — plus the three things a replacement can get wrong and a creation cannot: leaving a stale edge
behind, churning a row that did not move, and closing a loop that the creation path is checked for
and the edit path used not to be.

The outbox assertion lives on ``TransactionTestCase``, for the reason CLAUDE.md rule 15 gives:
``TestCase`` never commits, so an event asserted there looks exactly like an event nobody will
receive.
"""

import json
from typing import Any

from django.test import Client, TestCase, TransactionTestCase

from apps.accounts.tests.support import bearer
from apps.activity.models import ActivityRecord
from apps.events.models import OutboxEvent
from apps.events.tests.celery_support import EagerCeleryMixin
from apps.events.tests.registry_support import only_handlers
from apps.portfolio.tests.scenario import (
    OWNER_CODE,
    PROJECT_CODE,
    SECOND_PROJECT_CODE,
    PortfolioScenario,
)
from apps.work.models import TaskDependency

TASKS_URL = f"/api/v1/projects/{PROJECT_CODE}/tasks"
OTHER_TASKS_URL = f"/api/v1/projects/{SECOND_PROJECT_CODE}/tasks"

PROSE = "Esperar la firma del contrato marco"


class TaskDependencyEditingTestCase(TestCase):
    """``PATCH /api/v1/tasks/{code}`` with ``depends_on`` — the list replaces the set."""

    scenario: PortfolioScenario
    auth: dict[str, str]

    def setUp(self) -> None:
        self.scenario = PortfolioScenario()
        self.scenario.add_task_workflow()
        self.auth = bearer(username=OWNER_CODE)
        self.first = _create_task(self.auth, title="Prepare the environment")
        self.second = _create_task(self.auth, title="Migrate the data")
        self.subject = _create_task(self.auth, title="Cut the release")

    def test_a_task_created_without_prerequisites_gains_them(self) -> None:
        response = self._patch(depends_on=[self.first])

        self.assertEqual(response.status_code, 200)
        self.assertEqual(_dependency_codes(response), [self.first])

    def test_a_shorter_list_removes_the_edge_it_leaves_out(self) -> None:
        self._patch(depends_on=[self.first, self.second])

        response = self._patch(depends_on=[self.first])

        self.assertEqual(_dependency_codes(response), [self.first])
        self.assertEqual(TaskDependency.objects.for_project(PROJECT_CODE).count(), 1)

    def test_a_different_list_replaces_the_whole_set_rather_than_adding_to_it(self) -> None:
        self._patch(depends_on=[self.first])

        response = self._patch(depends_on=[self.second])

        self.assertEqual(_dependency_codes(response), [self.second])

    def test_an_empty_list_clears_every_prerequisite(self) -> None:
        self._patch(depends_on=[self.first, PROSE])

        response = self._patch(depends_on=[])

        self.assertEqual(response.json()["dependencies"], [])
        self.assertEqual(TaskDependency.objects.for_project(PROJECT_CODE).count(), 0)

    def test_an_absent_field_leaves_the_prerequisites_alone(self) -> None:
        self._patch(depends_on=[self.first])

        response = self._patch(title="Cut the release, finally")

        self.assertEqual(response.json()["title"], "Cut the release, finally")
        self.assertEqual(_dependency_codes(response), [self.first])

    def test_a_null_reads_as_absent_because_the_empty_list_is_how_a_set_is_cleared(self) -> None:
        self._patch(depends_on=[self.first])

        response = self._patch(depends_on=None)

        self.assertEqual(_dependency_codes(response), [self.first])

    def test_prose_that_is_not_a_task_code_is_kept_verbatim_instead_of_404ing(self) -> None:
        # The normal case, not the exception: 61 of the 82 source tasks name their prerequisite in
        # words. Rejecting them would delete the operation's own notes.
        response = self._patch(depends_on=[PROSE])

        self.assertEqual(response.json()["dependencies"], [{"task_code": None, "raw_label": PROSE}])

    def test_re_sending_an_unchanged_edge_keeps_its_row_rather_than_recreating_it(self) -> None:
        self._patch(depends_on=[self.first])
        original = TaskDependency.objects.get(task__code=self.subject)

        self._patch(depends_on=[self.first, self.second])

        kept = TaskDependency.objects.get(task__code=self.subject, depends_on__code=self.first)
        # Same row, so "waiting on this since the 3rd" survives an edit that added a second one.
        self.assertEqual(kept.pk, original.pk)
        self.assertEqual(kept.created_at, original.created_at)

    def test_re_sending_the_identical_set_changes_nothing_and_records_nothing(self) -> None:
        self._patch(depends_on=[self.first])
        ActivityRecord.objects.all().delete()

        self._patch(depends_on=[self.first])

        self.assertFalse(ActivityRecord.objects.filter(verb="DEPENDENCIES_CHANGED").exists())

    def test_a_prerequisite_that_would_close_a_loop_is_refused_with_409(self) -> None:
        # ``first`` already waits on ``subject``; making ``subject`` wait on ``first`` closes it.
        self._patch(code=self.first, depends_on=[self.subject])

        response = self._patch(depends_on=[self.first])

        self.assertEqual(response.status_code, 409)
        body = response.json()
        self.assertEqual(body["code"], "conflicting_state")
        self.assertEqual(body["details"]["cycle"], [self.subject, self.first, self.subject])
        self.assertEqual(TaskDependency.objects.filter(task__code=self.subject).count(), 0)

    def test_a_task_may_not_be_made_to_wait_on_itself(self) -> None:
        response = self._patch(depends_on=[self.subject])

        self.assertEqual(response.status_code, 409)

    def test_breaking_the_edge_first_is_what_makes_the_reverse_one_legal(self) -> None:
        # The check reads the graph as it stands *after* the replacement, so the loop is refused
        # while the edge exists and accepted once the edit that removed it has landed.
        self._patch(code=self.first, depends_on=[self.subject])
        self._patch(code=self.first, depends_on=[])

        response = self._patch(depends_on=[self.first])

        self.assertEqual(response.status_code, 200)
        self.assertEqual(_dependency_codes(response), [self.first])

    def test_a_task_code_from_another_project_is_refused(self) -> None:
        outsider = _create_task(self.auth, title="Someone else's work", url=OTHER_TASKS_URL)

        response = self._patch(depends_on=[outsider])

        self.assertEqual(response.status_code, 422)
        self.assertIn("depends_on", response.json()["details"]["fields"])
        self.assertEqual(TaskDependency.objects.filter(task__code=self.subject).count(), 0)

    def test_a_task_code_that_names_nothing_is_404(self) -> None:
        response = self._patch(depends_on=["PRJ-T1-T99"])

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["code"], "not_found")

    def test_a_refused_replacement_leaves_the_previous_set_untouched(self) -> None:
        self._patch(depends_on=[self.first])

        self._patch(depends_on=[self.second, "PRJ-T1-T99"])

        self.assertEqual(
            [row.depends_on.code for row in TaskDependency.objects.for_project(PROJECT_CODE)],
            [self.first],
        )

    def test_redrawing_the_set_records_it_against_the_task(self) -> None:
        self._patch(depends_on=[self.first, PROSE])

        record = ActivityRecord.objects.get(verb="DEPENDENCIES_CHANGED")
        # On the task, unlike a removal: a prerequisite is a statement about this piece of work.
        self.assertEqual(record.entity_type, "task")
        self.assertEqual(record.entity_id, self.subject)
        self.assertEqual(record.from_value, "")
        self.assertEqual(record.to_value, f"{self.first}, {PROSE}")
        # The full lists live in JSONB, so nothing is lost to the 255-character columns.
        self.assertEqual(record.metadata["before"], [])
        self.assertEqual(record.metadata["after"], [self.first, PROSE])
        self.assertEqual(record.metadata["project_code"], PROJECT_CODE)

    def _patch(self, *, code: str | None = None, **body: Any) -> Any:
        return self.client.patch(
            f"/api/v1/tasks/{code or self.subject}",
            data=json.dumps(body),
            content_type="application/json",
            **self.auth,
        )


class TaskDependencyOutboxTestCase(EagerCeleryMixin, TransactionTestCase):
    """A dependency edit really commits ``task.updated``, on the topic that already exists."""

    def setUp(self) -> None:
        super().setUp()
        self.scenario = PortfolioScenario()
        self.scenario.add_task_workflow()
        self.auth = bearer(username=OWNER_CODE)
        self.first = _create_task(self.auth, title="Prepare the environment")
        self.subject = _create_task(self.auth, title="Cut the release")

    def test_the_change_travels_inside_task_updated_as_two_lists(self) -> None:
        with only_handlers():
            self.client.patch(
                f"/api/v1/tasks/{self.subject}",
                data=json.dumps({"depends_on": [self.first, PROSE]}),
                content_type="application/json",
                **self.auth,
            )

        event = OutboxEvent.objects.get(topic="task.updated", entity_id=self.subject)
        self.assertEqual(event.payload["project_code"], PROJECT_CODE)
        # Arrays and not scalars: a set-valued field's honest before/after is the set, and a
        # consumer asking "did it stop waiting on that one" must not have to re-read the graph.
        self.assertEqual(
            event.payload["changes"]["depends_on"],
            {"from": [], "to": [self.first, PROSE]},
        )

    def test_a_set_that_did_not_move_commits_no_event(self) -> None:
        with only_handlers():
            self.client.patch(
                f"/api/v1/tasks/{self.subject}",
                data=json.dumps({"depends_on": []}),
                content_type="application/json",
                **self.auth,
            )

        self.assertFalse(OutboxEvent.objects.filter(topic="task.updated").exists())


def _dependency_codes(response: Any) -> list[str | None]:
    """The resolved prerequisites of a task detail response, in the order it renders them."""
    return [dependency["task_code"] for dependency in response.json()["dependencies"]]


def _create_task(auth: dict[str, str], *, title: str, url: str = TASKS_URL) -> str:
    """Create one task through the route that allocates its code, and return that code."""
    response = Client().post(
        url,
        data=json.dumps({"title": title, "priority": "critica", "assignee": OWNER_CODE}),
        content_type="application/json",
        **auth,
    )
    return str(response.json()["code"])
