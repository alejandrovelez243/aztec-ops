"""HTTP behaviour of the project routes, and of the one error mapping they all share.

These are ``TestCase`` and not ``SimpleTestCase`` because a route is only interesting against a
database: what is being proved is that an illegal transition comes back as the **documented 409
with the legal moves attached**, not as a 500, and that requires a real workflow graph to be
illegal against.

The outbox is deliberately not asserted on here. These tests run inside ``TestCase``'s
never-committed transaction, so an ``on_commit`` assertion would pass while proving nothing
(CLAUDE.md rule 15); the outbox rows these routes write are covered by the consumer tests, which
are ``TransactionTestCase``.
"""

import json
from typing import Any

from django.test import TestCase

from apps.accounts.models import User
from apps.accounts.tests.support import bearer, make_member
from apps.activity.models import ActivityRecord
from apps.catalog.models import Role
from apps.portfolio.tests.scenario import OWNER_CODE, PROJECT_CODE, PortfolioScenario


class ProjectDetailRouteTestCase(TestCase):
    scenario: PortfolioScenario
    auth: dict[str, str]

    @classmethod
    def setUpTestData(cls) -> None:
        cls.scenario = PortfolioScenario()
        cls.scenario.add_transitions()
        cls.auth = bearer(username=OWNER_CODE)

    def test_detail_returns_the_legal_transitions_the_frontend_renders_buttons_from(self) -> None:
        response = self.client.get(f"/api/v1/projects/{PROJECT_CODE}", **self.auth)

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(
            [option["to_state"]["code"] for option in body["transitions"]], ["blocked"]
        )
        self.assertTrue(body["transitions"][0]["requires_reason"])

    def test_detail_states_are_delivered_with_their_category_not_only_their_code(self) -> None:
        body = self.client.get(f"/api/v1/projects/{PROJECT_CODE}", **self.auth).json()

        self.assertEqual(body["state"]["code"], "execution")
        self.assertEqual(body["state"]["category"], "IN_PROGRESS")

    def test_unknown_project_is_the_documented_not_found_envelope(self) -> None:
        response = self.client.get("/api/v1/projects/PRJ-NOPE", **self.auth)

        self.assertEqual(response.status_code, 404)
        self.assertEqual(
            response.json()["code"],
            "not_found",
        )
        self.assertEqual(response.json()["details"], {"entity": "project", "id": "PRJ-NOPE"})

    def test_reading_a_project_requires_a_token_like_every_other_route(self) -> None:
        response = self.client.get(f"/api/v1/projects/{PROJECT_CODE}")

        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["code"], "authentication_required")


class ProjectTransitionRouteTestCase(TestCase):
    scenario: PortfolioScenario
    auth: dict[str, str]

    @classmethod
    def setUpTestData(cls) -> None:
        cls.scenario = PortfolioScenario()
        cls.scenario.add_transitions()
        cls.auth = bearer(username=OWNER_CODE)

    def _transition(self, **body: Any) -> Any:
        return self.client.post(
            f"/api/v1/projects/{PROJECT_CODE}/transition",
            data=json.dumps(body),
            content_type="application/json",
            **self.auth,
        )

    def test_a_declared_move_returns_the_project_with_its_new_transitions(self) -> None:
        response = self._transition(to_state="blocked", reason="Waiting on client access.")

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["state"]["code"], "blocked")
        # No edge leaves ``blocked`` in this graph, so the button list is legitimately empty —
        # which is the answer the UI must render, not a reason to invent a move.
        self.assertEqual(body["transitions"], [])

    def test_an_undeclared_move_is_409_and_names_the_moves_that_are_legal(self) -> None:
        response = self._transition(to_state="discovery", reason="Back to the drawing board.")

        self.assertEqual(response.status_code, 409)
        body = response.json()
        self.assertEqual(body["code"], "transition_not_allowed")
        self.assertEqual(body["details"]["from_state"], "execution")
        self.assertEqual(body["details"]["allowed"], ["blocked"])

    def test_a_missing_reason_on_an_edge_that_requires_one_is_422_naming_the_field(self) -> None:
        response = self._transition(to_state="blocked")

        self.assertEqual(response.status_code, 422)
        body = response.json()
        self.assertEqual(body["code"], "validation_error")
        self.assertIn("reason", body["details"]["fields"])

    def test_the_state_did_not_move_when_the_transition_was_refused(self) -> None:
        self._transition(to_state="blocked")

        detail = self.client.get(f"/api/v1/projects/{PROJECT_CODE}", **self.auth).json()
        self.assertEqual(detail["state"]["code"], "execution")


class MutationAuthenticationTestCase(TestCase):
    """A write is attributed to a verified account, or it does not happen."""

    scenario: PortfolioScenario
    auth: dict[str, str]

    @classmethod
    def setUpTestData(cls) -> None:
        cls.scenario = PortfolioScenario()
        cls.scenario.add_transitions()
        cls.auth = bearer(username=OWNER_CODE)

    def _transition(self, **headers: str) -> Any:
        return self.client.post(
            f"/api/v1/projects/{PROJECT_CODE}/transition",
            data=json.dumps({"to_state": "blocked", "reason": "Because."}),
            content_type="application/json",
            **headers,
        )

    def test_a_mutating_request_without_a_credential_is_401(self) -> None:
        response = self._transition()

        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["code"], "authentication_required")

    def test_a_forged_token_is_refused_with_the_code_that_means_refresh_me(self) -> None:
        response = self._transition(HTTP_AUTHORIZATION="Bearer not.a.token")

        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["code"], "invalid_token")

    def test_the_activity_record_names_the_signed_in_account_not_a_claim(self) -> None:
        self._transition(**self.auth)

        record = ActivityRecord.objects.filter(verb=ActivityRecord.Verb.STATE_CHANGED).first()
        self.assertIsNotNone(record)
        self.assertEqual(record.actor if record else None, OWNER_CODE)


class ProjectWriteRouteTestCase(TestCase):
    scenario: PortfolioScenario
    auth: dict[str, str]

    @classmethod
    def setUpTestData(cls) -> None:
        cls.scenario = PortfolioScenario()
        cls.scenario.add_transitions()
        cls.auth = bearer(username=OWNER_CODE)

    def test_creating_a_project_allocates_its_code_and_its_initial_state(self) -> None:
        response = self.client.post(
            "/api/v1/projects",
            data=json.dumps(
                {
                    "name": "Contract intake automation",
                    "client": "atlas",
                    "engagement_type": "proyecto",
                    "owner": OWNER_CODE,
                    "business_value": 18000,
                }
            ),
            content_type="application/json",
            **self.auth,
        )

        self.assertEqual(response.status_code, 201)
        body = response.json()
        # The scenario's codes are ``PRJ-T1``/``PRJ-T2``, which carry no number, so the
        # allocator starts the numeric series at one rather than colliding with them.
        self.assertEqual(body["code"], "PRJ-01")
        self.assertEqual(body["state"]["code"], "discovery")

    def test_an_unknown_taxonomy_code_is_422_naming_the_field_that_failed(self) -> None:
        response = self.client.post(
            "/api/v1/projects",
            data=json.dumps(
                {"name": "Nope", "client": "atlas", "engagement_type": "does-not-exist"}
            ),
            content_type="application/json",
            **self.auth,
        )

        self.assertEqual(response.status_code, 422)
        self.assertIn("engagement_type", response.json()["details"]["fields"])

    def test_patching_only_the_fields_that_were_sent_leaves_the_rest_alone(self) -> None:
        response = self.client.patch(
            f"/api/v1/projects/{PROJECT_CODE}",
            data=json.dumps({"next_step": "Confirm repository access."}),
            content_type="application/json",
            **self.auth,
        )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["next_step"], "Confirm repository access.")
        self.assertEqual(body["name"], "Primary test project")

    def test_an_explicit_null_clears_a_nullable_field(self) -> None:
        response = self.client.patch(
            f"/api/v1/projects/{PROJECT_CODE}",
            data=json.dumps({"target_date": None}),
            content_type="application/json",
            **self.auth,
        )

        self.assertIsNone(response.json()["target_date"])

    def test_workflow_state_is_not_a_field_of_the_update_payload(self) -> None:
        self.client.patch(
            f"/api/v1/projects/{PROJECT_CODE}",
            data=json.dumps({"workflow_state": "blocked"}),
            content_type="application/json",
            **self.auth,
        )

        detail = self.client.get(f"/api/v1/projects/{PROJECT_CODE}", **self.auth).json()
        self.assertEqual(detail["state"]["code"], "execution")


class TeamLoadRouteTestCase(TestCase):
    scenario: PortfolioScenario
    auth: dict[str, str]

    @classmethod
    def setUpTestData(cls) -> None:
        cls.scenario = PortfolioScenario()
        cls.auth = bearer(username=OWNER_CODE)

    def test_a_person_carrying_nothing_is_reported_with_zeros_rather_than_omitted(self) -> None:
        response = self.client.get("/api/v1/team/load", **self.auth)

        self.assertEqual(response.status_code, 200)
        rows = response.json()["items"]
        self.assertEqual([row["alias"] for row in rows], [OWNER_CODE])
        self.assertEqual(rows[0]["open_tasks"], 0)
        self.assertEqual(rows[0]["projects_owned"], 2)

    def test_utilization_is_load_over_capacity_and_flags_the_overloaded(self) -> None:
        for index in range(11):
            self.scenario.add_task(code=f"PRJ-T1-T{index:02d}")

        row = self.client.get("/api/v1/team/load", **self.auth).json()["items"][0]

        self.assertEqual(row["load_points"], 11)
        self.assertEqual(row["weekly_capacity_points"], 10)
        self.assertTrue(row["is_overloaded"])

    def test_a_row_carries_the_role_code_the_edit_dialog_sends_back(self) -> None:
        row = self.client.get("/api/v1/team/load", **self.auth).json()["items"][0]

        # Both, and for different readers: the label is rendered, the code is resent. A client
        # deriving one from the other would be comparing against operator-editable Spanish.
        self.assertIn("role", row)
        self.assertIn("role_code", row)
        self.assertTrue(row["is_active"])


class RosterFilterTestCase(TestCase):
    """Search, facets and ordering on ``GET /api/v1/team/load``.

    Built on its own roster rather than on :class:`PortfolioScenario`'s single owner, because
    every assertion here is about which people come back and in what order.
    """

    auth: dict[str, str]

    @classmethod
    def setUpTestData(cls) -> None:
        cls.delivery = Role.objects.create(code="delivery", label="Delivery", order=1)
        cls.commercial = Role.objects.create(code="commercial", label="Commercial", order=2)
        cls.auth = bearer(username=make_member(code="reader").code)
        User.objects.filter(code="reader").update(role=cls.delivery)
        cls._person(code="ana.ruiz", alias="Ana Ruiz", role=cls.delivery)
        cls._person(code="bruno.paz", alias="Bruno Paz", role=cls.commercial)
        cls._person(code="carla.vega", alias="Carla Vega", role=None, is_active=False)

    @classmethod
    def _person(cls, *, code: str, alias: str, role: Role | None, is_active: bool = True) -> User:
        return User.objects.create(
            username=code, code=code, alias=alias, role=role, is_active=is_active
        )

    def _aliases(self, query: str = "") -> list[str]:
        response = self.client.get(f"/api/v1/team/load{query}", **self.auth)
        self.assertEqual(response.status_code, 200)
        return [row["alias"] for row in response.json()["items"]]

    def test_the_default_is_the_active_roster_only(self) -> None:
        self.assertNotIn("carla.vega", self._aliases())

    def test_status_inactive_answers_the_question_a_boolean_flag_could_not(self) -> None:
        self.assertEqual(self._aliases("?status=inactive"), ["carla.vega"])

    def test_status_all_returns_both_halves(self) -> None:
        self.assertIn("carla.vega", self._aliases("?status=all"))

    def test_search_matches_the_display_name_case_insensitively(self) -> None:
        self.assertEqual(self._aliases("?q=bruno"), ["bruno.paz"])

    def test_search_also_matches_the_code_an_operator_half_remembers(self) -> None:
        self.assertEqual(self._aliases("?q=ana."), ["ana.ruiz"])

    def test_a_blank_search_shows_everybody_rather_than_appearing_to_filter(self) -> None:
        self.assertEqual(sorted(self._aliases("?q=")), sorted(self._aliases()))

    def test_the_role_facet_ors_its_values(self) -> None:
        aliases = self._aliases("?role=delivery&role=commercial")

        self.assertEqual(sorted(aliases), ["ana.ruiz", "bruno.paz", "reader"])

    def test_ordering_by_label_is_alphabetical_and_the_sign_reverses_it(self) -> None:
        ascending = self._aliases("?order_by=label")

        self.assertEqual(ascending, sorted(ascending))
        self.assertEqual(self._aliases("?order_by=-label"), list(reversed(ascending)))

    def test_people_tied_on_the_sort_key_keep_a_stable_order_between_reads(self) -> None:
        # Everybody here carries nothing, so utilization ties for all of them: without the
        # ``label`` tiebreaker the roster would shuffle between two reads of unchanged data.
        self.assertEqual(
            self._aliases("?order_by=-utilization"), self._aliases("?order_by=-utilization")
        )

    def test_an_order_by_outside_the_allowlist_is_422_and_says_what_is_allowed(self) -> None:
        response = self.client.get("/api/v1/team/load?order_by=salary", **self.auth)

        self.assertEqual(response.status_code, 422)
        body = response.json()
        self.assertEqual(body["code"], "validation_error")
        self.assertIn("utilization", body["details"]["allowed"])

    def test_overloaded_false_is_who_has_room_and_not_merely_the_absence_of_a_filter(self) -> None:
        # Nobody carries anything, so "has room" is everybody and "is overloaded" is nobody —
        # which is the pair a plain boolean could not express.
        self.assertEqual(self._aliases("?overloaded=true"), [])
        self.assertEqual(sorted(self._aliases("?overloaded=false")), sorted(self._aliases()))
