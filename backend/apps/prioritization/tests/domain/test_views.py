"""Reading a persisted breakdown document into the projection the API returns.

These are the rows that already exist. A document written before ``label`` was stored must still
produce a queue a Spanish-speaking operator can read, and one written by a policy version whose
signals have since been retired must still produce a rank that can be defended — neither may reach
the client as an exception or as an empty breakdown.
"""

from decimal import Decimal

from django.test import SimpleTestCase

from apps.prioritization.domain.views import ScoreSignalView, ScoreView


def document(*signals: dict[str, object]) -> dict[str, object]:
    """A minimal persisted breakdown carrying exactly the given signal lines."""
    return {
        "policy_version": "v1",
        "value": 61.4,
        "signals": list(signals),
        "modifiers": [],
        "flags": [],
        "computed_at": "2026-07-28T06:12:44+00:00",
    }


def line(**overrides: object) -> dict[str, object]:
    """One complete stored line, overriding only what the test is about."""
    return {
        "code": "overdue_work",
        "label": "Trabajo vencido",
        "raw": 0.5714,
        "weight": 0.2,
        "contribution": 11.43,
        "reason": "4 de 7 tareas abiertas pasaron su fecha.",
        **overrides,
    }


class StoredLabelTests(SimpleTestCase):
    """The label the engine wrote is the label the client is shown."""

    def test_the_stored_label_is_returned_verbatim(self) -> None:
        view = ScoreView.from_document(
            document(line(label="Trabajo vencido (v1)")),
            value=Decimal("61.40"),
            policy_version="v1",
        )
        self.assertEqual(view.breakdown[0].label, "Trabajo vencido (v1)")

    def test_a_stored_label_is_not_overwritten_by_the_registry(self) -> None:
        # The wording of the day is what the audit trail promises; a re-resolved caption would
        # rewrite a sentence whose facts were only true at computed_at.
        view = ScoreView.from_document(
            document(line(label="Retraso acumulado")),
            value=Decimal("61.40"),
            policy_version="v1",
        )
        self.assertEqual(view.breakdown[0].label, "Retraso acumulado")


class UnlabelledRowTests(SimpleTestCase):
    """A row written before labels existed still renders, and never raises."""

    def test_a_missing_label_is_resolved_from_the_registry(self) -> None:
        stored = line()
        del stored["label"]
        view = ScoreView.from_document(
            document(stored), value=Decimal("61.40"), policy_version="v1"
        )
        self.assertEqual(view.breakdown[0].label, "Trabajo vencido")

    def test_an_empty_label_is_resolved_from_the_registry(self) -> None:
        view = ScoreView.from_document(
            document(line(label="")), value=Decimal("61.40"), policy_version="v1"
        )
        self.assertEqual(view.breakdown[0].label, "Trabajo vencido")

    def test_a_retired_signal_falls_back_to_its_code_rather_than_disappearing(self) -> None:
        stored = line(code="team_morale")
        del stored["label"]
        view = ScoreView.from_document(
            document(stored), value=Decimal("61.40"), policy_version="v0"
        )
        # A dropped line would silently remove a contribution from the argument; the bare code is
        # visibly wrong instead, which is the correct alarm.
        self.assertEqual(view.breakdown[0].label, "team_morale")
        self.assertEqual(view.breakdown[0].contribution, 11.43)

    def test_a_line_with_neither_a_label_nor_a_code_is_skipped(self) -> None:
        stored = line()
        del stored["label"]
        del stored["code"]
        view = ScoreView.from_document(
            document(stored), value=Decimal("61.40"), policy_version="v1"
        )
        self.assertEqual(view.breakdown, ())

    def test_a_hand_edited_document_degrades_to_an_empty_breakdown(self) -> None:
        view = ScoreView.from_document("not a document", value=Decimal("0"), policy_version="")
        self.assertEqual(view.breakdown, ())


class DirectConstructionTests(SimpleTestCase):
    """The same captioning applies however a line is built, not only when parsed from JSONB."""

    def test_constructing_without_a_label_captions_from_the_registry(self) -> None:
        view = ScoreSignalView(
            code="blockage", raw=1.0, weight=0.15, contribution=15.0, reason="Sin bloqueos."
        )
        self.assertEqual(view.label, "Bloqueo")
