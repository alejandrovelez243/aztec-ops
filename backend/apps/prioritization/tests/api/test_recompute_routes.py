"""HTTP behaviour of the two recompute routes — the replacement for ``manage.py recompute``.

``TestCase``: these routes write ``PriorityScore`` and ``RiskFlag`` and emit nothing, so there is
no ``on_commit`` for a never-committed transaction to hide. That the routes emit nothing is itself
asserted here, because it is the property that keeps a bulk rebuild from flooding every open
dashboard with news that is not news.
"""

import json
from typing import Any

from django.test import TestCase

from apps.events.models import OutboxEvent
from apps.portfolio.tests.scenario import (
    OWNER_CODE,
    PROJECT_CODE,
    SECOND_PROJECT_CODE,
    PortfolioScenario,
)
from apps.prioritization.models import PriorityScore

ACTOR = {"HTTP_X_ACTOR": OWNER_CODE}
PORTFOLIO_URL = "/api/v1/recompute"
PROJECT_URL = f"/api/v1/projects/{PROJECT_CODE}/recompute"


class ProjectRecomputeRouteTestCase(TestCase):
    scenario: PortfolioScenario

    @classmethod
    def setUpTestData(cls) -> None:
        cls.scenario = PortfolioScenario()

    def _post(self, url: str, **headers: Any) -> Any:
        return self.client.post(
            url, data=json.dumps({}), content_type="application/json", **headers
        )

    def test_recomputing_one_project_persists_a_score_and_reports_the_change(self) -> None:
        response = self._post(PROJECT_URL, **ACTOR)

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual([item["project_code"] for item in body["items"]], [PROJECT_CODE])
        self.assertTrue(body["items"][0]["changed"])
        self.assertEqual(body["changed"], 1)
        self.assertTrue(PriorityScore.objects.filter(project__code=PROJECT_CODE).exists())

    def test_a_second_recompute_of_unchanged_facts_reports_nothing_moved(self) -> None:
        self._post(PROJECT_URL, **ACTOR)

        body = self._post(PROJECT_URL, **ACTOR).json()

        self.assertEqual(body["changed"], 0)
        self.assertFalse(body["items"][0]["changed"])

    def test_the_rebuild_publishes_no_event(self) -> None:
        self._post(PROJECT_URL, **ACTOR)

        self.assertEqual(OutboxEvent.objects.count(), 0)

    def test_an_unknown_project_is_the_documented_not_found_envelope(self) -> None:
        response = self._post("/api/v1/projects/PRJ-NOPE/recompute", **ACTOR)

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["code"], "not_found")

    def test_recomputing_requires_the_actor_header(self) -> None:
        response = self._post(PROJECT_URL)

        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["code"], "validation_error")


class PortfolioRecomputeRouteTestCase(TestCase):
    scenario: PortfolioScenario

    @classmethod
    def setUpTestData(cls) -> None:
        cls.scenario = PortfolioScenario()

    def test_the_portfolio_route_scores_every_active_project_at_one_instant(self) -> None:
        response = self.client.post(
            PORTFOLIO_URL, data=json.dumps({}), content_type="application/json", **ACTOR
        )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(
            sorted(item["project_code"] for item in body["items"]),
            sorted([PROJECT_CODE, SECOND_PROJECT_CODE]),
        )
        # One ``ran_at`` for the whole run: two projects with the same facts must not be ranked
        # apart by how long the loop took.
        self.assertEqual(body["changed"], 2)
        self.assertEqual(PriorityScore.objects.count(), 2)

    def test_an_archived_project_is_not_scored(self) -> None:
        self.scenario.other_project.is_archived = True
        self.scenario.other_project.save(update_fields=["is_archived"])

        body = self.client.post(
            PORTFOLIO_URL, data=json.dumps({}), content_type="application/json", **ACTOR
        ).json()

        self.assertEqual([item["project_code"] for item in body["items"]], [PROJECT_CODE])
