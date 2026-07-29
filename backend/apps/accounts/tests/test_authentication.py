"""What authentication and the two authorization rules actually guarantee over HTTP.

``TestCase`` throughout: every one of these needs an account row to sign in as, and none of them
touches the outbox or ``on_commit``, so the never-committed transaction hides nothing.

``GET /api/v1/catalog`` is the protected route under test wherever the *route* does not matter. It
is the cheapest authenticated read in the API — it needs no fixtures — and using a read is the
sharper proof: authentication being the API's default means the endpoints nobody would think to
protect are protected too.
"""

from typing import Any

from django.test import Client, TestCase

from apps.accounts.tests.support import TEST_PASSWORD, TOKEN_URL, bearer, make_member
from apps.portfolio.tests.scenario import OPS_LEAD_CODE, OWNER_CODE, PortfolioScenario

CATALOG_URL = "/api/v1/catalog"
REFRESH_URL = "/api/v1/auth/token/refresh"
LOGOUT_URL = "/api/v1/auth/logout"
ACCESS_COOKIE = "aztec_access"

#: Deliberately not :data:`TEST_PASSWORD`. Named rather than inlined so bandit's argument
#: heuristic has nothing to flag at the call site.
WRONG_PASSWORD = "definitely-not-it"  # noqa: S105


class UnauthenticatedRequestTestCase(TestCase):
    """Nothing outside the documented exception list answers without a credential."""

    @classmethod
    def setUpTestData(cls) -> None:
        make_member(code="member")

    def test_a_protected_read_without_a_credential_is_401(self) -> None:
        response = self.client.get(CATALOG_URL)

        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["code"], "authentication_required")

    def test_the_refusal_uses_the_one_documented_error_envelope(self) -> None:
        body = self.client.get(CATALOG_URL).json()

        self.assertEqual(sorted(body), ["code", "details", "message"])
        self.assertEqual(body["details"], {})

    def test_a_garbage_token_is_invalid_token_and_not_authentication_required(self) -> None:
        response = self.client.get(CATALOG_URL, HTTP_AUTHORIZATION="Bearer nonsense")

        self.assertEqual(response.status_code, 401)
        # A distinct code, because the client's next move differs: refresh, not sign in.
        self.assertEqual(response.json()["code"], "invalid_token")

    def test_the_event_stream_is_not_a_hole_in_the_wall(self) -> None:
        # A plain Django view, so it cannot inherit the API's default ``auth`` — it calls the same
        # function instead, and answers in the same envelope. Refused before Redis is touched.
        response = self.client.get("/api/stream")

        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["code"], "authentication_required")

    def test_the_health_probes_stay_open_because_a_probe_carries_no_token(self) -> None:
        for route in ("live", "ready", "pipeline"):
            with self.subTest(route=route):
                self.assertNotEqual(self.client.get(f"/api/v1/health/{route}").status_code, 401)


class TokenAcceptanceTestCase(TestCase):
    """The two ways a valid token is presented, and the one place the cookie is refused."""

    @classmethod
    def setUpTestData(cls) -> None:
        make_member(code="member")

    def test_a_valid_token_is_accepted_through_the_authorization_header(self) -> None:
        response = self.client.get(CATALOG_URL, **bearer(username="member"))

        self.assertEqual(response.status_code, 200)

    def test_signing_in_sets_the_httponly_cookie_an_eventsource_will_send(self) -> None:
        self.client.post(
            TOKEN_URL,
            data={"username": "member", "password": TEST_PASSWORD},
            content_type="application/json",
        )

        cookie = self.client.cookies[ACCESS_COOKIE]
        self.assertTrue(cookie["httponly"])
        self.assertEqual(cookie["samesite"], "Lax")
        # Scoped to the API, so ``/admin/`` and the static assets never receive it.
        self.assertEqual(cookie["path"], "/api/")

    def test_a_valid_token_is_accepted_through_the_cookie_alone(self) -> None:
        # This is the whole reason the cookie exists: ``EventSource`` cannot set a header, so a
        # GET carrying nothing but the cookie has to authenticate.
        self.client.post(
            TOKEN_URL,
            data={"username": "member", "password": TEST_PASSWORD},
            content_type="application/json",
        )

        response = self.client.get(CATALOG_URL)

        self.assertEqual(response.status_code, 200)

    def test_the_cookie_alone_cannot_authenticate_a_mutation(self) -> None:
        # Ninja routes are CSRF-exempt, so honouring the cookie on a POST would make every write
        # forgeable from any origin. Writes present the header, which no cross-origin page can set.
        self.client.post(
            TOKEN_URL,
            data={"username": "member", "password": TEST_PASSWORD},
            content_type="application/json",
        )

        response = self.client.post(
            "/api/v1/projects",
            data={"name": "Forged", "client": "atlas", "engagement_type": "proyecto"},
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["code"], "authentication_required")


class SignInTestCase(TestCase):
    """What the token endpoint answers, and what it refuses to reveal."""

    @classmethod
    def setUpTestData(cls) -> None:
        make_member(code="member")

    def _sign_in(self, **body: Any) -> Any:
        return self.client.post(
            TOKEN_URL,
            data={"username": "member", "password": TEST_PASSWORD, **body},
            content_type="application/json",
        )

    def test_a_pair_arrives_with_the_actor_and_the_capability_the_ui_renders_from(self) -> None:
        body = self._sign_in().json()

        self.assertTrue(body["access"])
        self.assertTrue(body["refresh"])
        self.assertGreater(body["expires_in"], 0)
        self.assertEqual(body["actor"]["alias"], "member")
        self.assertFalse(body["is_ops_lead"])

    def test_a_wrong_password_and_an_unknown_user_are_indistinguishable(self) -> None:
        wrong_password = self._sign_in(password=WRONG_PASSWORD).json()
        unknown_user = self._sign_in(username="ghost").json()

        self.assertEqual(wrong_password["code"], "invalid_credentials")
        # Identical bodies: this endpoint must not be usable to enumerate accounts.
        self.assertEqual(wrong_password, unknown_user)

    def test_a_deactivated_account_can_no_longer_sign_in(self) -> None:
        member = make_member(code="retired")
        member.is_active = False
        member.save(update_fields=["is_active"])

        response = self._sign_in(username="retired")

        self.assertEqual(response.status_code, 401)


class TokenRefreshTestCase(TestCase):
    """The refresh flow: a new access token without re-presenting a password."""

    @classmethod
    def setUpTestData(cls) -> None:
        make_member(code="member")

    def _refresh(self, refresh: str) -> Any:
        return self.client.post(
            REFRESH_URL, data={"refresh": refresh}, content_type="application/json"
        )

    def test_a_refresh_token_buys_a_working_access_token(self) -> None:
        pair = self.client.post(
            TOKEN_URL,
            data={"username": "member", "password": TEST_PASSWORD},
            content_type="application/json",
        ).json()

        grant = self._refresh(pair["refresh"]).json()

        self.assertNotIn("refresh", grant)
        self.assertEqual(grant["actor"]["alias"], "member")
        accepted = Client().get(CATALOG_URL, HTTP_AUTHORIZATION=f"Bearer {grant['access']}")
        self.assertEqual(accepted.status_code, 200)

    def test_refreshing_re_sets_the_cookie_so_an_open_stream_can_reconnect(self) -> None:
        pair = self.client.post(
            TOKEN_URL,
            data={"username": "member", "password": TEST_PASSWORD},
            content_type="application/json",
        ).json()
        self.client.cookies.clear()

        self._refresh(pair["refresh"])

        self.assertIn(ACCESS_COOKIE, self.client.cookies)

    def test_an_access_token_is_not_accepted_as_a_refresh_token(self) -> None:
        pair = self.client.post(
            TOKEN_URL,
            data={"username": "member", "password": TEST_PASSWORD},
            content_type="application/json",
        ).json()

        response = self._refresh(pair["access"])

        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["code"], "invalid_token")

    def test_a_nonsense_refresh_token_is_refused_rather_than_raising(self) -> None:
        self.assertEqual(self._refresh("not.a.token").status_code, 401)


class LogoutTestCase(TestCase):
    """Signing out removes the credential the browser sends by itself."""

    @classmethod
    def setUpTestData(cls) -> None:
        make_member(code="member")

    def test_logging_out_clears_the_cookie_and_the_stream_stops_authenticating(self) -> None:
        auth = bearer(username="member")
        self.client.post(
            TOKEN_URL,
            data={"username": "member", "password": TEST_PASSWORD},
            content_type="application/json",
        )

        self.client.post(LOGOUT_URL, **auth)

        self.assertFalse(self.client.cookies[ACCESS_COOKIE].value)
        self.assertEqual(self.client.get(CATALOG_URL).status_code, 401)

    def test_logging_out_is_itself_authenticated(self) -> None:
        response = self.client.post(LOGOUT_URL)

        self.assertEqual(response.status_code, 401)


class OpsLeadPermissionTestCase(TestCase):
    """The only two things being signed in is not enough for."""

    scenario: PortfolioScenario
    auth: dict[str, str]
    ops_auth: dict[str, str]

    @classmethod
    def setUpTestData(cls) -> None:
        cls.scenario = PortfolioScenario()
        cls.scenario.add_ops_lead()
        cls.auth = bearer(username=OWNER_CODE)
        cls.ops_auth = bearer(username=OPS_LEAD_CODE)

    def _override(self, **headers: str) -> Any:
        return self.client.post(
            f"/api/v1/projects/{self.scenario.project.code}/priority-override",
            data={"position": 1, "reason": "Escalated to the client's board."},
            content_type="application/json",
            **headers,
        )

    def test_a_member_who_is_not_an_ops_lead_is_refused_the_override_with_403(self) -> None:
        response = self._override(**self.auth)

        self.assertEqual(response.status_code, 403)
        body = response.json()
        self.assertEqual(body["code"], "permission_denied")
        self.assertEqual(body["details"]["required"], "ops_lead")

    def test_the_refusal_is_403_and_not_401_because_signing_in_again_cannot_help(self) -> None:
        self.assertEqual(self._override().status_code, 401)
        self.assertEqual(self._override(**self.auth).status_code, 403)

    def test_the_ops_lead_gets_past_the_permission_check(self) -> None:
        response = self._override(**self.ops_auth)

        # Whatever the use case then decides, it is no longer an authorization answer.
        self.assertNotIn(response.status_code, (401, 403))

    def test_every_other_write_is_collaborative_and_needs_no_role(self) -> None:
        # Aztec Ops is a Jira: any member may act on any project, including one they do not own.
        self.scenario.add_transitions()
        outsider = make_member(code="not.the.owner")

        response = self.client.post(
            f"/api/v1/projects/{self.scenario.project.code}/transition",
            data={"to_state": "blocked", "reason": "Client access still pending."},
            content_type="application/json",
            **bearer(username=outsider.code),
        )

        self.assertEqual(response.status_code, 200)
