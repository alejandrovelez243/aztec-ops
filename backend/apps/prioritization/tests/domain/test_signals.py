"""Boundary behaviour of the six signal strategies, with no database.

The asserted fragments are Spanish because the strings under test are: labels and reasons are the
copy the interface renders (PRODUCT.md), while everything naming the code stays English.
"""

from datetime import date, timedelta
from decimal import Decimal

from django.test import SimpleTestCase

from apps.prioritization.domain.registry import registered_signals
from apps.prioritization.domain.signals.blockage import AGE_SATURATION_DAYS, Blockage
from apps.prioritization.domain.signals.business_value import BusinessValue
from apps.prioritization.domain.signals.criticality import URGENT_SATURATION_TASKS, Criticality
from apps.prioritization.domain.signals.deadline_pressure import (
    NO_TARGET_DATE_SCORE,
    DeadlinePressure,
)
from apps.prioritization.domain.signals.overdue_work import OverdueWork
from apps.prioritization.domain.signals.staleness import Staleness

from .inputs import NOW, signal_input


class DeadlinePressureSignalTests(SimpleTestCase):
    """The 0.25 signal: distance to the committed date."""

    signal = DeadlinePressure()

    def test_missing_target_date_scores_the_midpoint_and_names_the_gap(self) -> None:
        result = self.signal.evaluate(signal_input())
        self.assertEqual(result.score, NO_TARGET_DATE_SCORE)
        self.assertIn("sin fecha comprometida", result.reason.lower())

    def test_overdue_target_date_saturates_and_names_the_days(self) -> None:
        result = self.signal.evaluate(signal_input(target_date=NOW.date() - timedelta(days=6)))
        self.assertEqual(result.score, 1.0)
        self.assertIn("pasó hace 6 día(s)", result.reason)

    def test_score_rises_as_the_target_date_approaches(self) -> None:
        previous = 0.0
        for days in (60, 30, 7, 1):
            with self.subTest(days=days):
                result = self.signal.evaluate(
                    signal_input(target_date=NOW.date() + timedelta(days=days))
                )
                self.assertGreaterEqual(result.score, previous)
                self.assertIn(f"Faltan {days} día(s)", result.reason)
                previous = result.score


class OverdueWorkSignalTests(SimpleTestCase):
    """The 0.20 signal: overdue tasks as a share of open tasks."""

    signal = OverdueWork()

    def test_no_open_tasks_scores_zero(self) -> None:
        result = self.signal.evaluate(signal_input())
        self.assertEqual(result.score, 0.0)
        self.assertIn("Sin tareas abiertas", result.reason)

    def test_ratio_is_reported_with_both_counts(self) -> None:
        result = self.signal.evaluate(signal_input(open_task_count=7, overdue_task_count=4))
        self.assertAlmostEqual(result.score, 0.5714, places=4)
        self.assertIn("4 de 7 tareas abiertas", result.reason)


class CriticalitySignalTests(SimpleTestCase):
    """The 0.15 signal: volume of open urgent work."""

    signal = Criticality()

    def test_no_urgent_work_scores_zero(self) -> None:
        result = self.signal.evaluate(signal_input())
        self.assertEqual(result.score, 0.0)
        self.assertIn("Sin tareas abiertas de prioridad urgente", result.reason)

    def test_saturates_at_the_named_threshold(self) -> None:
        for count, expected in ((3, 0.6), (URGENT_SATURATION_TASKS, 1.0), (9, 1.0)):
            with self.subTest(count=count):
                result = self.signal.evaluate(signal_input(urgent_open_task_count=count))
                self.assertEqual(result.score, expected)
                self.assertIn(f"{count} tarea(s) abierta(s)", result.reason)


class BusinessValueSignalTests(SimpleTestCase):
    """The 0.15 signal: contract value on a log scale."""

    signal = BusinessValue()

    def test_missing_value_does_not_raise_the_project(self) -> None:
        result = self.signal.evaluate(signal_input())
        self.assertEqual(result.score, 0.0)
        self.assertIn("Sin valor de contrato", result.reason)

    def test_log_scale_compresses_the_gap_between_a_small_and_a_large_contract(self) -> None:
        reference = Decimal("40000")
        small = self.signal.evaluate(
            signal_input(business_value=Decimal("8000"), portfolio_max_business_value=reference)
        )
        large = self.signal.evaluate(
            signal_input(business_value=Decimal("28000"), portfolio_max_business_value=reference)
        )
        self.assertLess(small.score, large.score)
        # 28k is 3.5x of 8k in money and well under 1.2x of it here: that compression is the point.
        self.assertLess(large.score / small.score, 1.2)
        self.assertIn("normalizado logarítmicamente", large.reason)

    def test_the_amount_is_written_in_spanish_notation(self) -> None:
        result = self.signal.evaluate(
            signal_input(
                business_value=Decimal("28000"),
                portfolio_max_business_value=Decimal("50000"),
            )
        )
        self.assertIn("28.000,00 USD", result.reason)
        self.assertIn("(50.000,00)", result.reason)


class BlockageSignalTests(SimpleTestCase):
    """The 0.15 signal: an older blocker scores higher, never lower."""

    signal = Blockage()

    def test_no_open_blockers_scores_zero(self) -> None:
        result = self.signal.evaluate(signal_input())
        self.assertEqual(result.score, 0.0)
        self.assertIn("Sin bloqueos abiertos", result.reason)

    def test_age_raises_the_score_up_to_saturation(self) -> None:
        for days, expected in ((0, 0.5), (20, 0.75), (AGE_SATURATION_DAYS, 1.0), (60, 1.0)):
            with self.subTest(days=days):
                result = self.signal.evaluate(
                    signal_input(open_blocker_count=1, oldest_blocker_age_days=days)
                )
                self.assertEqual(result.score, expected)
                self.assertIn(f"{days} día(s)", result.reason)


class StalenessSignalTests(SimpleTestCase):
    """The 0.10 signal: silence, worsened by the absence of a next step."""

    signal = Staleness()

    def test_never_recorded_activity_saturates(self) -> None:
        result = self.signal.evaluate(signal_input())
        self.assertEqual(result.score, 1.0)
        self.assertIn("Nunca se registró actividad", result.reason)

    def test_missing_next_step_adds_to_the_silence(self) -> None:
        with_step = self.signal.evaluate(
            signal_input(days_since_last_activity=7, next_step="Send the revised quote")
        )
        without_step = self.signal.evaluate(signal_input(days_since_last_activity=7))
        self.assertLess(with_step.score, without_step.score)
        self.assertIn("hay próximo paso definido", with_step.reason)
        self.assertIn("no hay próximo paso registrado", without_step.reason)

    def test_silence_past_the_threshold_saturates(self) -> None:
        result = self.signal.evaluate(
            signal_input(days_since_last_activity=30, next_step="Follow up")
        )
        self.assertEqual(result.score, 1.0)
        self.assertIn("30 día(s)", result.reason)


class EverySignalJustifiesItselfTests(SimpleTestCase):
    """A score without a reason is a bug, on every strategy and on the default input."""

    def test_every_registered_signal_returns_a_sentence(self) -> None:
        for code, strategy in registered_signals().items():
            with self.subTest(signal=code):
                result = strategy.evaluate(signal_input(target_date=date(2026, 8, 30)))
                self.assertGreaterEqual(len(result.reason), 10)
                self.assertTrue(result.reason.endswith("."))


class EverySignalNamesItselfTests(SimpleTestCase):
    """A seventh signal must reach the interface named, without a frontend change.

    The registry is the whole assertion: this passes for a signal that does not exist yet, and
    fails the moment one is registered without a label — which is the only place that omission can
    still be caught before it renders as ``overdue_work`` in a Spanish queue.
    """

    def test_every_registered_signal_carries_a_label_that_is_not_its_code(self) -> None:
        for code, strategy in registered_signals().items():
            with self.subTest(signal=code):
                self.assertTrue(strategy.label.strip())
                self.assertNotEqual(strategy.label, code)

    def test_the_labels_are_the_interface_language(self) -> None:
        labels = {code: strategy.label for code, strategy in registered_signals().items()}
        self.assertEqual(
            labels,
            {
                "deadline_pressure": "Presión de fecha",
                "overdue_work": "Trabajo vencido",
                "criticality": "Criticidad",
                "business_value": "Valor de negocio",
                "blockage": "Bloqueo",
                "staleness": "Inactividad",
            },
        )
