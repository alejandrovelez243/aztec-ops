"""Rendering of before/after pairs for the ``*.updated`` topics, with no database."""

from datetime import UTC, date, datetime
from decimal import Decimal

from django.test import SimpleTestCase

from apps.events.domain.changes import render_change_value


class RenderChangeValueTestCase(SimpleTestCase):
    def test_json_native_values_pass_through_untouched(self) -> None:
        self.assertEqual(render_change_value("Reproduce on pilot"), "Reproduce on pilot")
        self.assertEqual(render_change_value(3), 3)
        self.assertEqual(render_change_value(1.5), 1.5)
        self.assertIs(render_change_value(True), True)

    def test_none_stays_null_instead_of_becoming_an_empty_string(self) -> None:
        self.assertIsNone(render_change_value(None))

    def test_decimal_becomes_a_float(self) -> None:
        rendered = render_change_value(Decimal("28000.50"))

        self.assertIsInstance(rendered, float)
        self.assertEqual(rendered, 28000.50)

    def test_date_and_datetime_become_iso_strings(self) -> None:
        self.assertEqual(render_change_value(date(2026, 7, 10)), "2026-07-10")
        self.assertEqual(
            render_change_value(datetime(2026, 7, 10, 9, 31, tzinfo=UTC)),
            "2026-07-10T09:31:00+00:00",
        )

    def test_referenced_row_is_rendered_by_its_business_code_not_its_primary_key(self) -> None:
        class _Priority:
            pk = 4
            code = "critica"

        self.assertEqual(render_change_value(_Priority()), "critica")

    def test_unrecognised_object_falls_back_to_str_so_the_payload_stays_serializable(self) -> None:
        class _Odd:
            def __str__(self) -> str:
                return "odd"

        self.assertEqual(render_change_value(_Odd()), "odd")
