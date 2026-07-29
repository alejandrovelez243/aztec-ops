"""How a written prerequisite is read as either a reference or the operation's words.

``SimpleTestCase`` on purpose: ``DependencySpec`` is pure domain, and a base class that
forbids database access is what keeps it that way (CLAUDE.md rule 15).
"""

from django.test import SimpleTestCase

from apps.work.domain.commands import DependencySpec


class DependencySpecFromTextTestCase(SimpleTestCase):
    """The classifier behind ``POST /api/v1/projects/{code}/tasks``'s ``depends_on``."""

    def test_a_task_code_is_read_as_a_reference_to_that_task(self) -> None:
        spec = DependencySpec.from_text("PRJ-15-T02")

        self.assertEqual(spec.depends_on_code, "PRJ-15-T02")
        self.assertEqual(spec.raw_label, "PRJ-15-T02")

    def test_a_lowercase_code_still_resolves_because_codes_are_stored_upper(self) -> None:
        spec = DependencySpec.from_text("prj-15-t02")

        self.assertEqual(spec.depends_on_code, "PRJ-15-T02")

    def test_surrounding_space_does_not_stop_a_code_from_resolving(self) -> None:
        spec = DependencySpec.from_text("  PRJ-15-T02  ")

        self.assertEqual(spec.depends_on_code, "PRJ-15-T02")

    def test_prose_is_kept_verbatim_and_resolves_to_no_task(self) -> None:
        """The normal case: 61 of the 82 source tasks describe their prerequisite."""
        spec = DependencySpec.from_text("acuerdo verbal con el cliente")

        self.assertIsNone(spec.depends_on_code)
        self.assertEqual(spec.raw_label, "acuerdo verbal con el cliente")

    def test_a_project_code_is_prose_here_because_a_task_cannot_depend_on_a_project(
        self,
    ) -> None:
        spec = DependencySpec.from_text("PRJ-15")

        self.assertIsNone(spec.depends_on_code)
        self.assertEqual(spec.raw_label, "PRJ-15")

    def test_something_code_shaped_but_not_a_task_stays_prose(self) -> None:
        for text in ("PRJ-15-T", "T02", "PRJ-15-X02", "-T02"):
            with self.subTest(text=text):
                self.assertIsNone(DependencySpec.from_text(text).depends_on_code)

    def test_an_entry_that_is_only_space_is_refused_rather_than_stored_empty(self) -> None:
        """The database constraint is ``depends_on_id IS NOT NULL OR raw_label <> ''``."""
        with self.assertRaises(ValueError):
            DependencySpec.from_text("   ")
