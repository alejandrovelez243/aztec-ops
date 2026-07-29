"""Paging and ordering arithmetic, with no database.

``SimpleTestCase`` on purpose: it *forbids* database access, so these tests prove that the shared
read-parameter helpers are pure. If one of them ever grows a query, the base class fails the test
rather than a reviewer having to notice.
"""

from django.test import SimpleTestCase

from apps.shared.ordering import UnknownOrdering, resolve_ordering
from apps.shared.pagination import MAX_PAGE_SIZE, PageWindow

ALLOWLIST = {"score": "priority_score", "name": "name"}


class PageWindowTestCase(SimpleTestCase):
    def test_the_first_page_starts_at_the_beginning(self) -> None:
        window = PageWindow.of(page=1, page_size=25)

        self.assertEqual((window.offset, window.limit), (0, 25))

    def test_the_offset_is_the_pages_already_skipped(self) -> None:
        window = PageWindow.of(page=4, page_size=25)

        self.assertEqual(window.offset, 75)

    def test_a_page_size_above_the_cap_is_clamped_and_not_rejected(self) -> None:
        window = PageWindow.of(page=1, page_size=5000)

        self.assertEqual(window.limit, MAX_PAGE_SIZE)

    def test_page_zero_reads_as_the_first_page_rather_than_a_negative_offset(self) -> None:
        window = PageWindow.of(page=0, page_size=10)

        self.assertEqual(window.offset, 0)


class ResolveOrderingTestCase(SimpleTestCase):
    def test_a_plain_field_sorts_ascending_and_appends_the_tiebreaker(self) -> None:
        self.assertEqual(
            resolve_ordering("name", ALLOWLIST, tiebreaker="project_code"),
            ["name", "project_code"],
        )

    def test_a_signed_field_sorts_descending_on_the_mapped_column(self) -> None:
        self.assertEqual(
            resolve_ordering("-score", ALLOWLIST, tiebreaker="project_code"),
            ["-priority_score", "project_code"],
        )

    def test_the_tiebreaker_is_always_last_so_a_page_boundary_cannot_repeat_a_row(self) -> None:
        for order_by in ("name", "-name", "score", "-score"):
            with self.subTest(order_by=order_by):
                self.assertEqual(
                    resolve_ordering(order_by, ALLOWLIST, tiebreaker="project_code")[-1],
                    "project_code",
                )

    def test_a_field_outside_the_allowlist_is_refused_and_reports_what_is_allowed(self) -> None:
        with self.assertRaises(UnknownOrdering) as raised:
            resolve_ordering("password", ALLOWLIST, tiebreaker="project_code")

        self.assertEqual(raised.exception.field_name, "password")
        self.assertEqual(set(raised.exception.allowed), {"score", "name"})

    def test_the_sign_is_not_part_of_the_name_being_checked(self) -> None:
        with self.assertRaises(UnknownOrdering) as raised:
            resolve_ordering("-password", ALLOWLIST, tiebreaker="project_code")

        self.assertEqual(raised.exception.field_name, "password")
