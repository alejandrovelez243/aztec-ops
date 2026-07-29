"""What editing the role vocabulary over HTTP guarantees.

``TestCase`` throughout: these routes write no events, so nothing here depends on a commit that
``TestCase``'s transaction never performs. That absence is itself asserted below — a role is picker
vocabulary, and a topic nobody could act on is a broadcast for its own sake.

The authorization tests go through the routes rather than the services, because the rule lives on
the route declaration (``auth=roster_admin``) and a test that called the service directly would
keep passing on the day somebody drops it.
"""

from typing import Any

from django.test import TestCase

from apps.accounts.tests.support import bearer, make_member
from apps.activity.models import ActivityRecord
from apps.catalog.models import Role
from apps.events.models import OutboxEvent

ROLES_URL = "/api/v1/catalog/roles"
CATALOG_URL = "/api/v1/catalog"

LEAD_CODE = "catalog.lead"
PLAIN_CODE = "catalog.member"


class RoleCreationTestCase(TestCase):
    """Adding a role, and the one way it is refused."""

    auth: dict[str, str]

    @classmethod
    def setUpTestData(cls) -> None:
        make_member(code=LEAD_CODE, is_ops_lead=True)
        cls.auth = bearer(username=LEAD_CODE)

    def _create(self, **overrides: Any) -> Any:
        payload = {"code": "design", "label": "Diseño", **overrides}
        return self.client.post(
            ROLES_URL, data=payload, content_type="application/json", **self.auth
        )

    def test_a_new_role_comes_back_as_the_reference_every_picker_renders(self) -> None:
        response = self._create()

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json(), {"code": "design", "label": "Diseño", "color": None})

    def test_a_new_role_is_active_and_last_in_the_operator_s_ordering(self) -> None:
        Role.objects.create(code="delivery", label="Delivery", order=7)

        self._create()

        role = Role.objects.get(code="design")
        self.assertTrue(role.is_active)
        # Last, so the thing just added is where the reader looks for it.
        self.assertGreater(role.order, 7)

    def test_a_taken_code_is_a_conflict_and_not_a_second_row(self) -> None:
        self._create()

        response = self._create(label="Otra cosa")

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["code"], "conflicting_state")
        self.assertEqual(Role.objects.filter(code="design").count(), 1)

    def test_a_retired_role_still_holds_its_code(self) -> None:
        Role.objects.create(code="design", label="Diseño", is_active=False)

        response = self._create()

        # The row exists and is merely out of the pickers, so the fix is to restore it rather
        # than create a second one that would resolve ambiguously ever after.
        self.assertEqual(response.status_code, 409)

    def test_creating_a_role_is_recorded_in_the_trail(self) -> None:
        self._create()

        record = ActivityRecord.objects.get(entity_type="role", entity_id="design")
        self.assertEqual(record.verb, "CREATED")
        self.assertEqual(record.actor, LEAD_CODE)
        self.assertEqual(record.to_value, "Diseño")

    def test_no_event_is_emitted_for_a_vocabulary_change(self) -> None:
        self._create()

        # A role is picker vocabulary: nothing recomputes, no read model denormalizes it, and the
        # surfaces that render one refetch the catalog.
        self.assertFalse(OutboxEvent.objects.filter(entity_type="role").exists())


class RoleUpdateTestCase(TestCase):
    """Renaming, retiring and restoring — and what each one does to everybody classified."""

    auth: dict[str, str]

    @classmethod
    def setUpTestData(cls) -> None:
        make_member(code=LEAD_CODE, is_ops_lead=True)
        cls.auth = bearer(username=LEAD_CODE)

    def setUp(self) -> None:
        self.role = Role.objects.create(code="design", label="Diseño", order=1)

    def _patch(self, **payload: Any) -> Any:
        return self.client.patch(
            f"{ROLES_URL}/design",
            data=payload,
            content_type="application/json",
            **self.auth,
        )

    def test_renaming_changes_the_label_and_never_the_code(self) -> None:
        response = self._patch(label="Diseño de producto")

        self.assertEqual(response.status_code, 200)
        self.role.refresh_from_db()
        self.assertEqual(self.role.label, "Diseño de producto")
        self.assertEqual(self.role.code, "design")

    def test_retiring_takes_it_out_of_the_pickers_without_deleting_it(self) -> None:
        self._patch(is_active=False)

        self.assertTrue(Role.objects.filter(code="design").exists())
        roles = self.client.get(CATALOG_URL, **self.auth).json()["roles"]
        self.assertNotIn("design", [role["code"] for role in roles])

    def test_a_person_classified_under_a_retired_role_keeps_it(self) -> None:
        member = make_member(code="ana.perez")
        member.role = self.role
        member.save(update_fields=["role"])

        self._patch(is_active=False)

        member.refresh_from_db()
        # Retiring is not deleting: nobody is silently unclassified.
        self.assertEqual(member.role, self.role)

    def test_restoring_puts_it_back_in_the_pickers(self) -> None:
        self._patch(is_active=False)

        self._patch(is_active=True)

        roles = self.client.get(CATALOG_URL, **self.auth).json()["roles"]
        self.assertIn("design", [role["code"] for role in roles])

    def test_each_direction_is_recorded_under_its_own_verb(self) -> None:
        self._patch(label="Diseño de producto")
        self._patch(is_active=False)

        verbs = list(
            ActivityRecord.objects.filter(entity_id="design")
            .order_by("id")
            .values_list("verb", flat=True)
        )
        self.assertEqual(verbs, ["RENAMED", "DEACTIVATED"])

    def test_sending_the_values_it_already_has_records_nothing(self) -> None:
        response = self._patch(label="Diseño", is_active=True)

        self.assertEqual(response.status_code, 200)
        self.assertFalse(ActivityRecord.objects.filter(entity_id="design").exists())

    def test_editing_a_role_that_does_not_exist_is_a_404(self) -> None:
        response = self.client.patch(
            f"{ROLES_URL}/nope",
            data={"label": "X"},
            content_type="application/json",
            **self.auth,
        )

        # 404 rather than the 422 its ``UnknownCode`` base carries: the client addressed a
        # resource that is not there, which is a different problem from the engine failing to
        # resolve a code it read off a project.
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["details"]["entity"], "role")


class RoleListTestCase(TestCase):
    """The editor's view of the vocabulary, which the pickers deliberately are not."""

    auth: dict[str, str]

    @classmethod
    def setUpTestData(cls) -> None:
        make_member(code=LEAD_CODE, is_ops_lead=True)
        cls.auth = bearer(username=LEAD_CODE)
        Role.objects.create(code="delivery", label="Delivery", order=1)
        Role.objects.create(code="design", label="Diseño", order=2, is_active=False)

    def _codes(self, query: str = "") -> list[str]:
        response = self.client.get(f"{ROLES_URL}{query}", **self.auth)
        self.assertEqual(response.status_code, 200)
        return [role["code"] for role in response.json()]

    def test_the_default_is_everything_including_what_was_retired(self) -> None:
        # The whole reason this route exists: a screen that offers "retirar" and cannot show
        # what is retired offers a delete with better manners.
        self.assertEqual(self._codes(), ["delivery", "design"])

    def test_the_catalog_still_shows_only_what_may_be_picked(self) -> None:
        roles = self.client.get(CATALOG_URL, **self.auth).json()["roles"]

        self.assertEqual([role["code"] for role in roles], ["delivery"])

    def test_each_half_can_be_asked_for_on_its_own(self) -> None:
        self.assertEqual(self._codes("?status=active"), ["delivery"])
        self.assertEqual(self._codes("?status=inactive"), ["design"])

    def test_reading_the_editor_s_list_is_an_ops_lead_s(self) -> None:
        make_member(code=PLAIN_CODE)

        response = self.client.get(ROLES_URL, **bearer(username=PLAIN_CODE))

        # Which roles were retired is not something a reader needs in order to read the roster.
        self.assertEqual(response.status_code, 403)


class RoleAuthorizationTestCase(TestCase):
    """Reading the vocabulary is everybody's business; writing it is an ops lead's."""

    member_auth: dict[str, str]

    @classmethod
    def setUpTestData(cls) -> None:
        make_member(code=PLAIN_CODE)
        Role.objects.create(code="delivery", label="Delivery")
        cls.member_auth = bearer(username=PLAIN_CODE)

    def test_an_ordinary_member_may_read_the_catalog(self) -> None:
        response = self.client.get(CATALOG_URL, **self.member_auth)

        self.assertEqual(response.status_code, 200)

    def test_an_ordinary_member_may_not_add_a_role(self) -> None:
        response = self.client.post(
            ROLES_URL,
            data={"code": "design", "label": "Diseño"},
            content_type="application/json",
            **self.member_auth,
        )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["code"], "permission_denied")
        self.assertFalse(Role.objects.filter(code="design").exists())

    def test_an_ordinary_member_may_not_retire_one(self) -> None:
        response = self.client.patch(
            f"{ROLES_URL}/delivery",
            data={"is_active": False},
            content_type="application/json",
            **self.member_auth,
        )

        self.assertEqual(response.status_code, 403)
        self.assertTrue(Role.objects.get(code="delivery").is_active)
