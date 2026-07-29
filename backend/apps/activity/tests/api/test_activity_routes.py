"""HTTP behaviour of ``GET /api/v1/activity``, the portfolio-wide feed.

The service tests say what each facet selects. What is left to prove here is the wiring — that
each query parameter reaches the facet with its own name, that a repeated ``verb`` arrives as a
list rather than as its last value, and that the paging contract of `docs/API.md` §1.3 holds at
the boundary: a page size above the cap is clamped, an unknown facet value is a 200 with nothing
in it, and no credential is a 401 before any of that is even considered.
"""

from urllib.parse import urlencode

from django.test import TestCase

from apps.accounts.tests.support import bearer, make_member
from apps.activity.models import ActivityRecord
from apps.activity.tests.trail import (
    BLOCKED_AT,
    CAMILA,
    DARIO,
    DECISION_ID,
    MOVED_AT,
    OTHER_PROJECT_CODE,
    PROJECT_CODE,
    SYSTEM_ACTOR,
    TASK_CODE,
    Trail,
)
from apps.shared.pagination import MAX_PAGE_SIZE

FEED_URL = "/api/v1/activity"


class PortfolioActivityAuthenticationTestCase(TestCase):
    """The trail names people and clients, so reading it requires being signed in."""

    def test_a_request_without_a_credential_is_refused_before_the_feed_is_read(self) -> None:
        response = self.client.get(FEED_URL)

        self.assertEqual(response.status_code, 401)


class PortfolioActivityFeedTestCase(TestCase):
    """The signed-in read: the envelope, the order and the subjects it spans."""

    trail: Trail
    auth: dict[str, str]

    @classmethod
    def setUpTestData(cls) -> None:
        make_member(code=CAMILA)
        cls.trail = Trail()
        cls.auth = bearer(username=CAMILA)

    def _feed(self, query: str = "") -> dict[str, object]:
        response = self.client.get(f"{FEED_URL}{query}", **self.auth)
        self.assertEqual(response.status_code, 200)
        return response.json()

    def test_the_feed_returns_the_paginated_envelope_newest_first(self) -> None:
        body = self._feed()

        self.assertEqual(tuple(item["id"] for item in body["items"]), self.trail.newest_first())
        self.assertEqual(body["count"], 6)

    def test_an_item_carries_the_subject_that_tells_one_row_from_another(self) -> None:
        newest = self._feed()["items"][0]

        self.assertEqual(newest["entity_type"], ActivityRecord.EntityType.PROJECT)
        self.assertEqual(newest["entity_id"], PROJECT_CODE)
        self.assertEqual(newest["verb"], ActivityRecord.Verb.PRIORITY_CHANGED)
        self.assertEqual(newest["origin"], ActivityRecord.Origin.POLICY)
        self.assertEqual(newest["actor"], SYSTEM_ACTOR)
        self.assertEqual(newest["correlation_id"], str(DECISION_ID))

    def test_the_feed_carries_projects_tasks_and_blockers_at_once(self) -> None:
        body = self._feed()

        self.assertEqual(
            {item["entity_type"] for item in body["items"]},
            {
                ActivityRecord.EntityType.PROJECT,
                ActivityRecord.EntityType.TASK,
                ActivityRecord.EntityType.BLOCKER,
            },
        )
        self.assertIn(TASK_CODE, {item["entity_id"] for item in body["items"]})
        self.assertIn(OTHER_PROJECT_CODE, {item["entity_id"] for item in body["items"]})

    def test_each_query_parameter_reaches_its_own_facet(self) -> None:
        cases = {
            "entity_type": ("?entity_type=blocker", (self.trail.blocker_raised.id,)),
            "entity_id": (
                f"?entity_id={TASK_CODE}",
                (self.trail.task_added.id,),
            ),
            "verb": ("?verb=STATE_CHANGED", (self.trail.moved.id,)),
            "actor": (
                f"?actor={DARIO}",
                (self.trail.blocker_raised.id, self.trail.task_added.id),
            ),
            "origin": (
                "?origin=MANUAL",
                (self.trail.blocker_raised.id, self.trail.moved.id),
            ),
            "since": (
                f"?{urlencode({'since': BLOCKED_AT.isoformat()})}",
                (self.trail.lowered.id, self.trail.raised.id, self.trail.blocker_raised.id),
            ),
            "until": (
                f"?{urlencode({'until': MOVED_AT.isoformat()})}",
                (self.trail.moved.id, self.trail.created.id),
            ),
            "correlation_id": (
                f"?correlation_id={DECISION_ID}",
                (self.trail.lowered.id, self.trail.raised.id),
            ),
        }
        for facet, (query, expected) in cases.items():
            with self.subTest(facet=facet):
                body = self._feed(query)

                self.assertEqual(tuple(item["id"] for item in body["items"]), expected)
                self.assertEqual(body["count"], len(expected))

    def test_a_repeated_verb_ors_its_values_rather_than_keeping_the_last_one(self) -> None:
        body = self._feed("?verb=STATE_CHANGED&verb=BLOCKER_RAISED")

        self.assertEqual(
            tuple(item["id"] for item in body["items"]),
            (self.trail.blocker_raised.id, self.trail.moved.id),
        )
        self.assertEqual(body["count"], 2)

    def test_facets_given_together_narrow_together(self) -> None:
        body = self._feed(
            "?verb=PRIORITY_CHANGED&verb=BLOCKER_RAISED&origin=POLICY"
            f"&{urlencode({'since': BLOCKED_AT.isoformat()})}"
        )

        self.assertEqual(
            tuple(item["id"] for item in body["items"]),
            (self.trail.lowered.id, self.trail.raised.id),
        )
        self.assertEqual(body["count"], 2)


class PortfolioActivityUnknownFacetTestCase(TestCase):
    """A saved filter naming a retired value must not break a read-only screen."""

    auth: dict[str, str]

    @classmethod
    def setUpTestData(cls) -> None:
        make_member(code=CAMILA)
        Trail()
        cls.auth = bearer(username=CAMILA)

    def test_an_unknown_facet_value_is_an_empty_page_and_never_a_422(self) -> None:
        for facet, query in {
            "verb": "?verb=RETIRED_VERB",
            "actor": "?actor=nobody",
            "entity_type": "?entity_type=engagement",
            "origin": "?origin=ORACLE",
            "entity_id": "?entity_id=PRJ-NOPE",
        }.items():
            with self.subTest(facet=facet):
                response = self.client.get(f"{FEED_URL}{query}", **self.auth)

                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json(), {"items": [], "count": 0})

    def test_a_malformed_value_of_a_typed_facet_is_still_a_422(self) -> None:
        # The vocabularies are open; the *types* are not. A correlation_id that is not a UUID is a
        # client bug rather than a question the trail can answer.
        response = self.client.get(f"{FEED_URL}?correlation_id=not-a-uuid", **self.auth)

        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["code"], "validation_error")


class PortfolioActivityPagingTestCase(TestCase):
    """§1.3 at the boundary: pages partition the feed, an oversized page size is clamped."""

    trail: Trail
    auth: dict[str, str]

    @classmethod
    def setUpTestData(cls) -> None:
        make_member(code=CAMILA)
        cls.trail = Trail()
        cls.auth = bearer(username=CAMILA)

    def _items(self, query: str) -> tuple[int, ...]:
        response = self.client.get(f"{FEED_URL}{query}", **self.auth)
        self.assertEqual(response.status_code, 200)
        return tuple(item["id"] for item in response.json()["items"])

    def test_page_one_and_page_two_neither_overlap_nor_skip_a_row(self) -> None:
        first = self._items("?page=1&page_size=3")
        second = self._items("?page=2&page_size=3")

        self.assertEqual(first + second, self.trail.newest_first())
        self.assertEqual(set(first) & set(second), set())

    def test_count_stays_the_total_on_every_page(self) -> None:
        for page in (1, 2, 3):
            with self.subTest(page=page):
                body = self.client.get(f"{FEED_URL}?page={page}&page_size=3", **self.auth).json()

                self.assertEqual(body["count"], 6)

    def test_a_page_beyond_the_last_is_empty_and_still_reports_the_total(self) -> None:
        body = self.client.get(f"{FEED_URL}?page=9&page_size=3", **self.auth).json()

        self.assertEqual(body["items"], [])
        self.assertEqual(body["count"], 6)

    def test_a_page_size_above_the_cap_is_clamped_and_not_rejected(self) -> None:
        response = self.client.get(f"{FEED_URL}?page_size={MAX_PAGE_SIZE + 500}", **self.auth)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()["items"]), 6)

    def test_page_zero_is_refused_at_the_boundary_because_pages_start_at_one(self) -> None:
        # API.md §1.3 declares `page` as int >= 1, and the schema enforces it: PageWindow's own
        # clamp is the second line of defence for a direct caller, not the HTTP contract.
        response = self.client.get(f"{FEED_URL}?page=0", **self.auth)

        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["code"], "validation_error")


class EmptyPortfolioActivityFeedTestCase(TestCase):
    """A deployment with nothing recorded yet still answers the command center."""

    auth: dict[str, str]

    @classmethod
    def setUpTestData(cls) -> None:
        make_member(code=CAMILA)
        cls.auth = bearer(username=CAMILA)

    def test_an_empty_trail_is_an_empty_page_rather_than_an_error(self) -> None:
        response = self.client.get(FEED_URL, **self.auth)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"items": [], "count": 0})
