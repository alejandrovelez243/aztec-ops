"""HTTP behaviour of the two recompute routes — the replacement for ``manage.py recompute``.

``TestCase``: these routes write ``PriorityScore`` and emit nothing, so there is
no ``on_commit`` for a never-committed transaction to hide. That the routes emit nothing is itself
asserted here, because it is the property that keeps a bulk rebuild from flooding every open
dashboard with news that is not news.
"""

import json
from typing import Any

from django.test import TestCase

from apps.accounts.tests.support import bearer
from apps.events.models import OutboxEvent
from apps.portfolio.tests.scenario import (
    OPS_LEAD_CODE,
    OWNER_CODE,
    PROJECT_CODE,
    SECOND_PROJECT_CODE,
    PortfolioScenario,
)
from apps.prioritization.models import PriorityScore

PORTFOLIO_URL = "/api/v1/recompute"
PROJECT_URL = f"/api/v1/projects/{PROJECT_CODE}/recompute"


class ProjectRecomputeRouteTestCase(TestCase):
    """Rebuilding one project: open to any member, because its blast radius is one project."""

    scenario: PortfolioScenario
    auth: dict[str, str]

    @classmethod
    def setUpTestData(cls) -> None:
        cls.scenario = PortfolioScenario()
        cls.auth = bearer(username=OWNER_CODE)

    def _post(self, url: str, **headers: Any) -> Any:
        return self.client.post(
            url, data=json.dumps({}), content_type="application/json", **headers
        )

    def test_recomputing_one_project_persists_a_score_and_reports_the_change(self) -> None:
        response = self._post(PROJECT_URL, **self.auth)

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual([item["project_code"] for item in body["items"]], [PROJECT_CODE])
        self.assertTrue(body["items"][0]["changed"])
        self.assertEqual(body["changed"], 1)
        self.assertTrue(PriorityScore.objects.filter(project__code=PROJECT_CODE).exists())

    def test_a_second_recompute_of_unchanged_facts_reports_nothing_moved(self) -> None:
        self._post(PROJECT_URL, **self.auth)

        body = self._post(PROJECT_URL, **self.auth).json()

        self.assertEqual(body["changed"], 0)
        self.assertFalse(body["items"][0]["changed"])

    def test_the_rebuild_publishes_no_event(self) -> None:
        self._post(PROJECT_URL, **self.auth)

        self.assertEqual(OutboxEvent.objects.count(), 0)

    def test_an_unknown_project_is_the_documented_not_found_envelope(self) -> None:
        response = self._post("/api/v1/projects/PRJ-NOPE/recompute", **self.auth)

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["code"], "not_found")

    def test_recomputing_requires_a_token(self) -> None:
        response = self._post(PROJECT_URL)

        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["code"], "authentication_required")

    def test_an_ordinary_member_may_rebuild_one_project(self) -> None:
        self.assertEqual(self._post(PROJECT_URL, **self.auth).status_code, 200)


class PortfolioRecomputeRouteTestCase(TestCase):
    """Rebuilding the whole portfolio: ops lead only, because it is portfolio-wide and expensive."""

    scenario: PortfolioScenario
    auth: dict[str, str]
    ops_auth: dict[str, str]

    @classmethod
    def setUpTestData(cls) -> None:
        cls.scenario = PortfolioScenario()
        cls.scenario.add_ops_lead()
        cls.auth = bearer(username=OWNER_CODE)
        cls.ops_auth = bearer(username=OPS_LEAD_CODE)

    def _recompute_all(self, **headers: Any) -> Any:
        return self.client.post(
            PORTFOLIO_URL, data=json.dumps({}), content_type="application/json", **headers
        )

    def test_the_portfolio_route_scores_every_active_project_at_one_instant(self) -> None:
        response = self._recompute_all(**self.ops_auth)

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

        body = self._recompute_all(**self.ops_auth).json()

        self.assertEqual([item["project_code"] for item in body["items"]], [PROJECT_CODE])

    def test_a_member_who_is_not_an_ops_lead_is_refused_with_403(self) -> None:
        response = self._recompute_all(**self.auth)

        self.assertEqual(response.status_code, 403)
        body = response.json()
        self.assertEqual(body["code"], "permission_denied")
        self.assertEqual(body["details"]["required"], "ops_lead")
        # Refused after authentication, so nothing was rebuilt.
        self.assertEqual(PriorityScore.objects.count(), 0)

    def test_no_credential_at_all_is_401_rather_than_403(self) -> None:
        response = self._recompute_all()

        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["code"], "authentication_required")
