"""The weighted sum, the breakdown invariants, ``valid_until`` and the input hash."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from django.test import SimpleTestCase

from apps.prioritization.domain.errors import (
    PolicySignalMismatch,
    PolicyWeightsNotNormalized,
)
from apps.prioritization.domain.policies import load_policy
from apps.prioritization.domain.scoring import (
    FINAL_WEEK_DAYS,
    compute_breakdown,
    compute_input_hash,
    compute_valid_until,
)
from apps.prioritization.domain.specifications import evaluate_risk

from .inputs import NOW, signal_input

WEIGHTS = {
    "deadline_pressure": 0.25,
    "overdue_work": 0.20,
    "criticality": 0.15,
    "business_value": 0.15,
    "blockage": 0.15,
    "staleness": 0.10,
}


def active_policy(modifiers: dict[str, bool] | None = None) -> object:
    """Load the v1 criterion, optionally enabling the engagement type modifier."""
    return load_policy(version="v1", weights=WEIGHTS, modifiers=modifiers or {})


class PolicyLoadTests(SimpleTestCase):
    """A policy that disagrees with the registry never reaches the engine."""

    def test_weights_must_cover_every_registered_signal(self) -> None:
        with self.assertRaises(PolicySignalMismatch):
            load_policy(version="v2", weights={"deadline_pressure": 1.0}, modifiers={})

    def test_weights_must_sum_to_one(self) -> None:
        weights = dict(WEIGHTS, staleness=0.30)
        with self.assertRaises(PolicyWeightsNotNormalized):
            load_policy(version="v2", weights=weights, modifiers={})


class BreakdownInvariantTests(SimpleTestCase):
    """The document must explain the number it travels with."""

    def test_contributions_sum_to_the_base_and_the_value_is_the_modified_base(self) -> None:
        data = signal_input(
            target_date=NOW.date() - timedelta(days=6),
            open_task_count=7,
            overdue_task_count=4,
            urgent_open_task_count=3,
            open_blocker_count=1,
            oldest_blocker_age_days=19,
            business_value=Decimal("28000"),
            portfolio_max_business_value=Decimal("40000"),
            days_since_last_activity=10,
            next_step="Send the revised quote",
            engagement_type_code="proyecto",
            engagement_type_weight=Decimal("1.10"),
        )
        policy = active_policy({"engagement_type": True})
        breakdown = compute_breakdown(
            data=data, policy=policy, flags=evaluate_risk(data.as_risk_input())
        )

        for signal in breakdown.signals:
            with self.subTest(signal=signal.code):
                expected = round(Decimal(str(signal.raw)) * signal.weight * 100, 2)
                self.assertEqual(signal.contribution, expected)

        self.assertEqual(breakdown.base, sum(s.contribution for s in breakdown.signals))
        self.assertEqual(breakdown.value, round(breakdown.base * breakdown.modifier_total, 2))
        self.assertEqual(breakdown.modifier_total, Decimal("1.10"))

    def test_the_value_never_leaves_the_stated_range(self) -> None:
        data = signal_input(
            target_date=NOW.date() - timedelta(days=90),
            open_task_count=5,
            overdue_task_count=5,
            urgent_open_task_count=9,
            open_blocker_count=4,
            oldest_blocker_age_days=90,
            business_value=Decimal("100000"),
            portfolio_max_business_value=Decimal("40000"),
            engagement_type_code="proyecto",
            engagement_type_weight=Decimal("1.50"),
        )
        breakdown = compute_breakdown(
            data=data, policy=active_policy({"engagement_type": True}), flags=()
        )
        self.assertLessEqual(breakdown.value, Decimal("100"))
        self.assertGreaterEqual(breakdown.value, Decimal("0"))

    def test_the_document_sorts_signals_by_contribution_so_a_rank_can_be_read_aloud(self) -> None:
        data = signal_input(target_date=NOW.date() - timedelta(days=6), open_task_count=1)
        document = compute_breakdown(data=data, policy=active_policy(), flags=()).as_document()
        contributions = [entry["contribution"] for entry in document["signals"]]
        self.assertEqual(contributions, sorted(contributions, reverse=True))
        self.assertEqual(document["policy_version"], "v1")

    def test_every_stored_line_carries_the_label_of_the_signal_that_wrote_it(self) -> None:
        data = signal_input(target_date=NOW.date() - timedelta(days=6), open_task_count=1)
        document = compute_breakdown(data=data, policy=active_policy(), flags=()).as_document()
        labels = {entry["code"]: entry["label"] for entry in document["signals"]}
        self.assertEqual(labels["deadline_pressure"], "Presión de fecha")
        self.assertEqual(len(labels), len(WEIGHTS))


class ValidUntilTests(SimpleTestCase):
    """The instant the clock tick will select this project on."""

    def test_no_time_signal_can_move_without_a_date_or_any_activity(self) -> None:
        self.assertIsNone(compute_valid_until(signal_input()))

    def test_the_final_week_boundary_comes_before_the_target_date(self) -> None:
        target = NOW.date() + timedelta(days=30)
        valid_until = compute_valid_until(signal_input(target_date=target))
        expected = datetime(target.year, target.month, target.day, tzinfo=UTC) - timedelta(
            days=FINAL_WEEK_DAYS
        )
        self.assertEqual(valid_until, expected)

    def test_a_passed_boundary_is_never_returned(self) -> None:
        valid_until = compute_valid_until(signal_input(target_date=NOW.date() - timedelta(days=30)))
        self.assertIsNone(valid_until)


class InputHashTests(SimpleTestCase):
    """Equal facts on the same day must hash equally, or the recalculator stops being cheap."""

    def test_the_same_facts_later_the_same_day_hash_the_same(self) -> None:
        first = compute_input_hash(signal_input(open_task_count=3))
        later = compute_input_hash(
            signal_input(open_task_count=3).model_copy(update={"now": NOW + timedelta(hours=5)})
        )
        self.assertEqual(first, later)

    def test_a_changed_fact_changes_the_hash(self) -> None:
        self.assertNotEqual(
            compute_input_hash(signal_input(open_task_count=3)),
            compute_input_hash(signal_input(open_task_count=4)),
        )
