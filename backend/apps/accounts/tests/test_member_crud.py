"""What editing the roster over HTTP actually guarantees.

``TestCase`` for everything that asserts on a status code, a row or an activity record: none of
those needs a real commit, and the never-committed transaction hides nothing from them.

``TransactionTestCase`` for the one class that asserts on the outbox, because that is exactly where
``TestCase`` would prove nothing — it would show the ``OutboxEvent`` row inside a transaction that
is about to roll back, which is indistinguishable from an event nobody will ever deliver.

The authorization tests deliberately use the *routes* rather than calling the services: the rule
lives on the route declaration (``auth=roster_admin``), so a test that called the service directly
would keep passing on the day somebody drops the declaration.
"""

from typing import Any

from django.test import TestCase, TransactionTestCase

from apps.accounts.models import User
from apps.accounts.tests.support import TEST_PASSWORD, bearer, make_member
from apps.activity.models import ActivityRecord
from apps.catalog.models import Role
from apps.events.models import OutboxEvent

MEMBERS_URL = "/api/v1/team/members"

LEAD_CODE = "roster.lead"
PLAIN_CODE = "roster.member"

#: A password that satisfies Django's configured validators. Long, not in the common list, and not
#: derived from any of the codes above, so the similarity validator has nothing to say about it.
STRONG_PASSWORD = "granite-harbour-49"  # noqa: S105

#: Deliberately none of the above: short, numeric and common enough to trip three validators at
#: once, which is what the "every complaint, not the first" assertion needs.
WEAK_PASSWORD = "123"  # noqa: S105


def _seed_roles() -> None:
    """Create the two roles the seeded catalog has, so a role code in a test means something."""
    Role.objects.create(code="delivery", label="Delivery", order=1)
    Role.objects.create(code="commercial_delivery", label="Commercial / Delivery", order=2)


class MemberCreationTestCase(TestCase):
    """Registering a person, and the two ways it is refused."""

    auth: dict[str, str]

    @classmethod
    def setUpTestData(cls) -> None:
        _seed_roles()
        make_member(code=LEAD_CODE, is_ops_lead=True)
        cls.auth = bearer(username=LEAD_CODE)

    def _create(self, **overrides: Any) -> Any:
        payload = {
            "code": "nadia.perez",
            "label": "Nadia Perez",
            "role": "delivery",
            "weekly_capacity_points": 18,
            **overrides,
        }
        return self.client.post(
            MEMBERS_URL, data=payload, content_type="application/json", **self.auth
        )

    def test_a_registered_person_comes_back_with_the_role_reference_a_form_can_resend(self) -> None:
        response = self._create()

        self.assertEqual(response.status_code, 201)
        body = response.json()
        self.assertEqual(body["alias"], "nadia.perez")
        # The code, not the Spanish label: an edit dialog preselects its picker from this.
        self.assertEqual(body["role"]["code"], "delivery")
        self.assertEqual(body["weekly_capacity_points"], 18)

    def test_username_mirrors_code_so_the_person_signs_in_as_who_the_trail_names(self) -> None:
        self._create()

        member = User.objects.get(code="nadia.perez")
        self.assertEqual(member.username, "nadia.perez")

    def test_a_person_created_without_a_password_exists_and_cannot_sign_in(self) -> None:
        body = self._create().json()

        self.assertFalse(body["has_password"])
        self.assertFalse(User.objects.get(code="nadia.perez").has_usable_password())

    def test_a_supplied_password_is_hashed_and_never_echoed(self) -> None:
        body = self._create(password=STRONG_PASSWORD).json()

        self.assertTrue(body["has_password"])
        self.assertNotIn("password", body)
        member = User.objects.get(code="nadia.perez")
        self.assertNotEqual(member.password, STRONG_PASSWORD)
        self.assertTrue(member.check_password(STRONG_PASSWORD))

    def test_a_weak_password_is_refused_with_every_rule_it_broke(self) -> None:
        response = self._create(password=WEAK_PASSWORD)

        self.assertEqual(response.status_code, 422)
        body = response.json()
        self.assertEqual(body["code"], "validation_error")
        # Every complaint, not the first: a form that fixes one rule per round trip is a form
        # people work around by choosing something worse.
        self.assertGreater(len(body["details"]["fields"]["password"]), 1)
        self.assertFalse(User.objects.filter(code="nadia.perez").exists())

    def test_a_duplicate_code_is_a_conflict_and_not_a_second_account(self) -> None:
        self._create()

        response = self._create(label="Somebody Else")

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["code"], "conflicting_state")
        self.assertEqual(User.objects.filter(code="nadia.perez").count(), 1)

    def test_an_unknown_role_names_the_field_rather_than_answering_not_found(self) -> None:
        response = self._create(role="architecture")

        self.assertEqual(response.status_code, 422)
        self.assertIn("role", response.json()["details"]["fields"])

    def test_a_capacity_of_zero_is_refused_because_it_is_the_divisor_of_owner_load(self) -> None:
        response = self._create(weekly_capacity_points=0)

        self.assertEqual(response.status_code, 422)

    def test_creation_writes_one_activity_record_naming_the_new_person(self) -> None:
        self._create()

        record = ActivityRecord.objects.get(entity_type="member", entity_id="nadia.perez")
        self.assertEqual(record.verb, "CREATED")
        self.assertEqual(record.actor, LEAD_CODE)
        self.assertEqual(record.to_value, "Nadia Perez")


class MemberUpdateTestCase(TestCase):
    """Editing a person: what moves, what is recorded, and what a no-op costs."""

    auth: dict[str, str]

    @classmethod
    def setUpTestData(cls) -> None:
        _seed_roles()
        make_member(code=LEAD_CODE, is_ops_lead=True)
        cls.auth = bearer(username=LEAD_CODE)

    def setUp(self) -> None:
        self.member = User.objects.create(
            username="ivan.soto",
            code="ivan.soto",
            alias="Ivan Soto",
            role=Role.objects.get(code="delivery"),
            weekly_capacity_points=20,
        )

    def _patch(self, **payload: Any) -> Any:
        return self.client.patch(
            f"{MEMBERS_URL}/ivan.soto",
            data=payload,
            content_type="application/json",
            **self.auth,
        )

    def test_an_absent_field_is_left_untouched(self) -> None:
        self._patch(weekly_capacity_points=30)

        self.member.refresh_from_db()
        self.assertEqual(self.member.weekly_capacity_points, 30)
        # The role was not in the payload at all, so it must survive the edit.
        self.assertEqual(self.member.role.code if self.member.role else None, "delivery")

    def test_an_explicit_null_role_unclassifies_the_person(self) -> None:
        response = self._patch(role=None)

        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.json()["role"])
        self.member.refresh_from_db()
        self.assertIsNone(self.member.role)

    def test_each_moved_attribute_gets_its_own_verb_carrying_the_previous_value(self) -> None:
        self._patch(label="Ivan S. Soto", weekly_capacity_points=30)

        records = {
            record.verb: record for record in ActivityRecord.objects.filter(entity_id="ivan.soto")
        }
        self.assertEqual(sorted(records), ["CAPACITY_CHANGED", "RENAMED"])
        self.assertEqual(records["CAPACITY_CHANGED"].from_value, "20")
        self.assertEqual(records["CAPACITY_CHANGED"].to_value, "30")
        self.assertEqual(records["RENAMED"].from_value, "Ivan Soto")

    def test_one_edit_is_one_decision_so_its_records_share_a_correlation_id(self) -> None:
        self._patch(label="Ivan S. Soto", weekly_capacity_points=30)

        correlations = set(
            ActivityRecord.objects.filter(entity_id="ivan.soto").values_list(
                "correlation_id", flat=True
            )
        )
        self.assertEqual(len(correlations), 1)

    def test_sending_the_values_a_person_already_has_records_nothing(self) -> None:
        response = self._patch(label="Ivan Soto", weekly_capacity_points=20)

        self.assertEqual(response.status_code, 200)
        # A trail that records non-changes cannot be read for changes.
        self.assertFalse(ActivityRecord.objects.filter(entity_id="ivan.soto").exists())

    def test_editing_somebody_who_does_not_exist_is_a_404(self) -> None:
        response = self.client.patch(
            f"{MEMBERS_URL}/nobody",
            data={"label": "Nobody"},
            content_type="application/json",
            **self.auth,
        )

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["code"], "not_found")


class MemberDeactivationTestCase(TestCase):
    """Retiring somebody keeps their history and refuses to be done to oneself."""

    auth: dict[str, str]

    @classmethod
    def setUpTestData(cls) -> None:
        make_member(code=LEAD_CODE, is_ops_lead=True)
        make_member(code=PLAIN_CODE)
        cls.auth = bearer(username=LEAD_CODE)

    def test_delete_retires_the_person_and_deletes_no_row(self) -> None:
        response = self.client.delete(f"{MEMBERS_URL}/{PLAIN_CODE}", **self.auth)

        self.assertEqual(response.status_code, 204)
        member = User.objects.get(code=PLAIN_CODE)
        self.assertFalse(member.is_active)

    def test_retiring_writes_the_direction_as_the_verb(self) -> None:
        self.client.delete(f"{MEMBERS_URL}/{PLAIN_CODE}", **self.auth)

        record = ActivityRecord.objects.get(entity_id=PLAIN_CODE)
        self.assertEqual(record.verb, "DEACTIVATED")

    def test_retiring_somebody_already_retired_is_the_same_204_and_records_nothing_twice(
        self,
    ) -> None:
        self.client.delete(f"{MEMBERS_URL}/{PLAIN_CODE}", **self.auth)

        response = self.client.delete(f"{MEMBERS_URL}/{PLAIN_CODE}", **self.auth)

        self.assertEqual(response.status_code, 204)
        self.assertEqual(ActivityRecord.objects.filter(entity_id=PLAIN_CODE).count(), 1)

    def test_a_retired_person_is_restored_through_patch(self) -> None:
        self.client.delete(f"{MEMBERS_URL}/{PLAIN_CODE}", **self.auth)

        response = self.client.patch(
            f"{MEMBERS_URL}/{PLAIN_CODE}",
            data={"is_active": True},
            content_type="application/json",
            **self.auth,
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["is_active"])
        self.assertEqual(
            ActivityRecord.objects.filter(entity_id=PLAIN_CODE).latest("id").verb,
            "REACTIVATED",
        )

    def test_nobody_can_retire_their_own_account(self) -> None:
        response = self.client.delete(f"{MEMBERS_URL}/{LEAD_CODE}", **self.auth)

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["code"], "permission_denied")
        self.assertTrue(User.objects.get(code=LEAD_CODE).is_active)


class MemberPasswordTestCase(TestCase):
    """Replacing a credential: what it enables, and what it must never leave behind."""

    auth: dict[str, str]

    @classmethod
    def setUpTestData(cls) -> None:
        make_member(code=LEAD_CODE, is_ops_lead=True)
        cls.auth = bearer(username=LEAD_CODE)

    def setUp(self) -> None:
        self.member = User.objects.create(username="sin.clave", code="sin.clave", alias="Sin Clave")
        self.member.set_unusable_password()
        self.member.save(update_fields=["password"])

    def _set_password(self, password: str) -> Any:
        return self.client.post(
            f"{MEMBERS_URL}/sin.clave/password",
            data={"password": password},
            content_type="application/json",
            **self.auth,
        )

    def test_a_seeded_account_with_no_credential_becomes_able_to_sign_in(self) -> None:
        response = self._set_password(STRONG_PASSWORD)

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["has_password"])
        self.member.refresh_from_db()
        self.assertTrue(self.member.check_password(STRONG_PASSWORD))

    def test_the_trail_records_who_reset_it_and_never_what_it_was(self) -> None:
        self._set_password(STRONG_PASSWORD)

        record = ActivityRecord.objects.get(entity_id="sin.clave")
        self.assertEqual(record.verb, "PASSWORD_RESET")
        self.assertEqual(record.actor, LEAD_CODE)
        self.assertEqual(record.from_value, "")
        self.assertEqual(record.to_value, "")
        self.assertNotIn(STRONG_PASSWORD, str(record.metadata))

    def test_a_refused_password_leaves_the_account_as_it_was(self) -> None:
        response = self._set_password(WEAK_PASSWORD)

        self.assertEqual(response.status_code, 422)
        self.member.refresh_from_db()
        self.assertFalse(self.member.has_usable_password())


class RosterAuthorizationTestCase(TestCase):
    """Reading the roster is everybody's business; changing it is an ops lead's."""

    member_auth: dict[str, str]

    @classmethod
    def setUpTestData(cls) -> None:
        make_member(code=PLAIN_CODE)
        make_member(code=LEAD_CODE, is_ops_lead=True)
        cls.member_auth = bearer(username=PLAIN_CODE, password=TEST_PASSWORD)

    def test_an_ordinary_member_may_read_the_roster(self) -> None:
        response = self.client.get("/api/v1/team/load", **self.member_auth)

        self.assertEqual(response.status_code, 200)

    def test_an_ordinary_member_may_not_register_anybody(self) -> None:
        response = self.client.post(
            MEMBERS_URL,
            data={"code": "x.y", "label": "X Y", "weekly_capacity_points": 10},
            content_type="application/json",
            **self.member_auth,
        )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["code"], "permission_denied")
        # The 403 names the capability that was refused, not the URL that refused it.
        self.assertEqual(response.json()["details"]["required"], "ops_lead")
        self.assertIn("roster", response.json()["details"]["action"].lower())

    def test_an_ordinary_member_may_not_reset_a_password(self) -> None:
        response = self.client.post(
            f"{MEMBERS_URL}/{LEAD_CODE}/password",
            data={"password": STRONG_PASSWORD},
            content_type="application/json",
            **self.member_auth,
        )

        self.assertEqual(response.status_code, 403)

    def test_an_unauthenticated_caller_cannot_reach_the_roster_writes_at_all(self) -> None:
        response = self.client.post(
            MEMBERS_URL,
            data={"code": "x.y", "label": "X Y", "weekly_capacity_points": 10},
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 401)


class MemberOutboxTestCase(TransactionTestCase):
    """The events the roster writes actually commit.

    ``TransactionTestCase`` because this is the assertion ``TestCase`` cannot make: it would show
    the ``OutboxEvent`` row inside a transaction that never commits, which is exactly what an event
    nobody will ever deliver also looks like.
    """

    def setUp(self) -> None:
        _seed_roles()
        make_member(code=LEAD_CODE, is_ops_lead=True)
        self.auth = bearer(username=LEAD_CODE)

    def _topics_for(self, entity_id: str) -> list[str]:
        # Ordered by ``created_at`` and never by ``id``: the primary key is a UUID minted before
        # the insert, so ordering on it would shuffle the events into an order nothing produced.
        return list(
            OutboxEvent.objects.filter(entity_id=entity_id)
            .order_by("created_at")
            .values_list("topic", flat=True)
        )

    def test_registering_a_person_enqueues_member_created(self) -> None:
        self.client.post(
            MEMBERS_URL,
            data={"code": "eva.diaz", "label": "Eva Diaz", "weekly_capacity_points": 12},
            content_type="application/json",
            **self.auth,
        )

        self.assertEqual(self._topics_for("eva.diaz"), ["member.created"])

    def test_the_event_carries_the_business_code_and_never_a_primary_key(self) -> None:
        self.client.post(
            MEMBERS_URL,
            data={"code": "eva.diaz", "label": "Eva Diaz", "weekly_capacity_points": 12},
            content_type="application/json",
            **self.auth,
        )

        event = OutboxEvent.objects.get(entity_id="eva.diaz")
        self.assertEqual(event.entity_type, "member")
        self.assertEqual(event.payload["label"], "Eva Diaz")

    def test_an_edit_names_the_fields_that_moved(self) -> None:
        self.client.post(
            MEMBERS_URL,
            data={"code": "eva.diaz", "label": "Eva Diaz", "weekly_capacity_points": 12},
            content_type="application/json",
            **self.auth,
        )

        self.client.patch(
            f"{MEMBERS_URL}/eva.diaz",
            data={"weekly_capacity_points": 25, "role": "delivery"},
            content_type="application/json",
            **self.auth,
        )

        updated = OutboxEvent.objects.get(entity_id="eva.diaz", topic="member.updated")
        self.assertEqual(sorted(updated.payload["changed"]), ["role", "weekly_capacity_points"])

    def test_retiring_and_editing_in_one_request_emit_two_distinct_facts(self) -> None:
        self.client.post(
            MEMBERS_URL,
            data={"code": "eva.diaz", "label": "Eva Diaz", "weekly_capacity_points": 12},
            content_type="application/json",
            **self.auth,
        )

        self.client.patch(
            f"{MEMBERS_URL}/eva.diaz",
            data={"label": "Eva D.", "is_active": False},
            content_type="application/json",
            **self.auth,
        )

        self.assertEqual(
            self._topics_for("eva.diaz"),
            ["member.created", "member.updated", "member.activation_changed"],
        )

    def test_replacing_a_password_publishes_nothing(self) -> None:
        self.client.post(
            MEMBERS_URL,
            data={"code": "eva.diaz", "label": "Eva Diaz", "weekly_capacity_points": 12},
            content_type="application/json",
            **self.auth,
        )

        self.client.post(
            f"{MEMBERS_URL}/eva.diaz/password",
            data={"password": STRONG_PASSWORD},
            content_type="application/json",
            **self.auth,
        )

        # No ``member.password_reset`` topic exists, on purpose: broadcasting the timing of
        # credential changes to every open tab buys no subscriber anything.
        self.assertEqual(self._topics_for("eva.diaz"), ["member.created"])
