"""The six risk specifications, their composition, and the health derived from them."""

from datetime import timedelta

from django.test import SimpleTestCase

from apps.prioritization.domain.specifications import (
    HasNoNextStep,
    HasNoTargetDate,
    IsBlocked,
    IsOverdue,
    IsStale,
    OwnerOverloaded,
    derive_health,
    evaluate_risk,
)
from apps.prioritization.domain.types import Health, RiskFlag, Severity

from .inputs import NOW, risk_input


class IsBlockedTests(SimpleTestCase):
    """Blocked by the project's own state, by an open blocker, or by a blocked task."""

    specification = IsBlocked()

    def test_each_source_of_blockage_satisfies_the_same_flag(self) -> None:
        cases = (
            ("state", risk_input(state_category="BLOCKED")),
            ("blocker", risk_input(open_blocker_count=1, oldest_blocker_age_days=19)),
            ("task", risk_input(blocked_task_count=2)),
        )
        for source, data in cases:
            with self.subTest(source=source):
                self.assertTrue(self.specification.is_satisfied_by(data))

    def test_detail_names_the_blocker_age_when_a_blocker_is_open(self) -> None:
        data = risk_input(open_blocker_count=1, oldest_blocker_age_days=19)
        self.assertIn("19 días", self.specification.detail(data))

    def test_a_single_blocker_reads_in_the_singular(self) -> None:
        """Spanish agrees the adjective too, so "1 bloqueos abiertos" is wrong twice."""
        data = risk_input(open_blocker_count=1, oldest_blocker_age_days=1)
        self.assertIn("1 bloqueo abierto", self.specification.detail(data))
        self.assertIn("1 día", self.specification.detail(data))

    def test_a_quiet_project_is_not_blocked(self) -> None:
        self.assertFalse(
            self.specification.is_satisfied_by(risk_input(state_category="IN_PROGRESS"))
        )


class IsOverdueTests(SimpleTestCase):
    """Late by the project's own date, or by its tasks'."""

    specification = IsOverdue()

    def test_past_target_date_is_overdue_and_the_detail_counts_the_days(self) -> None:
        target_date = NOW.date() - timedelta(days=6)
        data = risk_input(target_date=target_date)
        self.assertTrue(self.specification.is_satisfied_by(data))
        self.assertIn("6 días de retraso", self.specification.detail(data))
        self.assertIn(target_date.isoformat(), self.specification.detail(data))

    def test_late_tasks_are_overdue_even_when_the_project_date_still_holds(self) -> None:
        data = risk_input(target_date=NOW.date() + timedelta(days=30), overdue_task_count=4)
        self.assertTrue(self.specification.is_satisfied_by(data))
        self.assertIn("4 tareas pasaron su fecha", self.specification.detail(data))


class HasNoNextStepTests(SimpleTestCase):
    """No written next step and nothing in progress."""

    specification = HasNoNextStep()

    def test_either_a_next_step_or_work_in_progress_clears_it(self) -> None:
        cases = (
            ("next step", risk_input(next_step="Send the revised quote")),
            ("in progress", risk_input(has_in_progress_task=True)),
        )
        for source, data in cases:
            with self.subTest(source=source):
                self.assertFalse(self.specification.is_satisfied_by(data))

    def test_whitespace_is_not_a_next_step(self) -> None:
        self.assertTrue(self.specification.is_satisfied_by(risk_input(next_step="   ")))


class HasNoTargetDateTests(SimpleTestCase):
    """The missing commitment on five of the twenty-two source projects."""

    specification = HasNoTargetDate()

    def test_a_null_target_date_is_the_finding(self) -> None:
        self.assertTrue(self.specification.is_satisfied_by(risk_input()))
        self.assertFalse(self.specification.is_satisfied_by(risk_input(target_date=NOW.date())))


class IsStaleTests(SimpleTestCase):
    """Silence measured against the operational threshold."""

    specification = IsStale()

    def test_threshold_is_the_boundary_and_never_happened_counts_as_stale(self) -> None:
        cases = ((None, True), (13, False), (14, True), (30, True))
        for days, expected in cases:
            with self.subTest(days=days):
                data = risk_input(days_since_last_activity=days, staleness_threshold_days=14)
                self.assertEqual(self.specification.is_satisfied_by(data), expected)


class OwnerOverloadedTests(SimpleTestCase):
    """Load against capacity, read and never counted."""

    specification = OwnerOverloaded()

    def test_an_unowned_project_cannot_be_overloaded(self) -> None:
        data = risk_input(owner_load_points=99, owner_capacity_points=0)
        self.assertFalse(self.specification.is_satisfied_by(data))

    def test_load_above_capacity_names_both_numbers(self) -> None:
        data = risk_input(owner_code="camila", owner_load_points=24, owner_capacity_points=20)
        self.assertTrue(self.specification.is_satisfied_by(data))
        self.assertIn("24 puntos", self.specification.detail(data))
        self.assertIn("20 puntos", self.specification.detail(data))
        self.assertIn("camila", self.specification.detail(data))


class SpecificationCompositionTests(SimpleTestCase):
    """The operators exist so a new condition never edits an existing one."""

    def test_and_or_not_compose_without_touching_the_operands(self) -> None:
        overdue_and_unblocked = IsOverdue() & ~IsBlocked()
        data = risk_input(target_date=NOW.date() - timedelta(days=2))
        self.assertTrue(overdue_and_unblocked.is_satisfied_by(data))

        blocked_or_overdue = IsBlocked() | IsOverdue()
        self.assertTrue(blocked_or_overdue.is_satisfied_by(data))
        self.assertIn("de retraso sobre la fecha objetivo", blocked_or_overdue.detail(data))

    def test_a_composite_names_itself_from_the_operands_it_was_built_from(self) -> None:
        """A composed specification is registrable, so it needs a label like any other."""
        self.assertEqual((IsBlocked() | IsOverdue()).label, "Bloqueado o Vencido")
        self.assertEqual((~IsBlocked()).label, "No bloqueado")


class RiskEvaluationTests(SimpleTestCase):
    """What the evaluator returns, and the health derived from it."""

    def test_an_archived_project_raises_nothing(self) -> None:
        data = risk_input(is_archived=True, state_category="BLOCKED", open_blocker_count=3)
        self.assertEqual(evaluate_risk(data), ())

    def test_every_raised_flag_carries_its_registry_severity_and_a_detail(self) -> None:
        data = risk_input(open_blocker_count=1, oldest_blocker_age_days=19)
        flags = evaluate_risk(data)
        codes = {flag.code for flag in flags}
        self.assertIn("BLOCKED", codes)
        for flag in flags:
            with self.subTest(flag=flag.code):
                self.assertIn(flag.severity, set(Severity))
                self.assertNotEqual(flag.detail, "")

    def test_every_raised_flag_is_named_so_no_client_has_to_map_the_code(self) -> None:
        """The label reaches the wire with the flag; that is what keeps rule 8 a backend change."""
        data = risk_input(open_blocker_count=2, oldest_blocker_age_days=19)
        for flag in evaluate_risk(data):
            with self.subTest(flag=flag.code):
                self.assertNotEqual(flag.label, "")
                self.assertNotEqual(flag.label, flag.code)
                self.assertEqual(flag.to_view().label, flag.label)

    def test_health_is_derived_from_severity_not_from_a_field(self) -> None:
        critical = (RiskFlag(code="BLOCKED", severity=Severity.CRITICAL, label="B", detail="."),)
        medium = (RiskFlag(code="STALE", severity=Severity.MEDIUM, label="I", detail="."),)
        self.assertEqual(derive_health(critical), Health.BLOCKED)
        self.assertEqual(derive_health(medium), Health.AT_RISK)
        self.assertEqual(derive_health(()), Health.HEALTHY)
