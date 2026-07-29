"""The two projections of a :class:`FieldChange`, with no database."""

from django.test import SimpleTestCase

from apps.work.domain.value_objects import FieldChange


class FieldChangeTestCase(SimpleTestCase):
    def test_payload_projection_keeps_the_events_md_from_to_shape(self) -> None:
        change = FieldChange(field="priority", before="alta", after="critica")

        self.assertEqual(change.as_payload(), {"from": "alta", "to": "critica"})

    def test_payload_projection_keeps_null_for_a_field_that_was_cleared(self) -> None:
        change = FieldChange(field="assignee", before="daniel.rojas", after=None)

        self.assertEqual(change.as_payload(), {"from": "daniel.rojas", "to": None})

    def test_audit_projection_renders_a_missing_side_as_the_empty_string(self) -> None:
        change = FieldChange(field="due_date", before=None, after="2026-08-14")

        self.assertEqual(change.from_value, "")
        self.assertEqual(change.to_value, "2026-08-14")

    def test_audit_projection_never_disagrees_with_the_payload_projection(self) -> None:
        change = FieldChange(field="assignee", before="daniel.rojas", after="camila.torres")

        self.assertEqual(change.from_value, change.as_payload()["from"])
        self.assertEqual(change.to_value, change.as_payload()["to"])
