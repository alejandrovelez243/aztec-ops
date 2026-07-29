"""Risk flags are a reading of the present, not a row somebody wrote in the past (ADR 0011).

``TestCase``: these are database reads with no outbox row and no ``on_commit`` hook in sight —
which is itself the claim under test. Nothing has to be delivered, recomputed or rebuilt for a
project to become overdue; the date passes and the next reader sees it.

The three cases below are the three surfaces a flag can reach a client through: the project detail,
served from the write side; the queue, served from a read-model row that may be days old; and the
facet the queue can be filtered by. All three are asserted at an instant *after* the last thing
that wrote anything, because the whole point of computing on read is that the gap does not matter.
"""

from datetime import UTC, datetime

from django.apps import apps
from django.test import TestCase

from apps.portfolio.domain.value_objects import SnapshotQueueFilters
from apps.portfolio.models import ProjectSnapshot
from apps.portfolio.services import read_project_detail, read_queue, rebuild_snapshot
from apps.portfolio.tests.scenario import PROJECT_CODE, PortfolioScenario
from apps.prioritization.models import PriorityScore

#: The scenario's projects are due 2026-12-31, so anything read at this instant is late. Chosen
#: rather than derived from ``timezone.now()`` so the assertion says the same thing every year.
AFTER_THE_TARGET_DATE = datetime(2027, 1, 15, 9, 0, tzinfo=UTC)

#: An instant *before* the target date, used to write a snapshot that could only have concluded
#: "not overdue" if it had concluded anything at all.
BEFORE_THE_TARGET_DATE = datetime(2026, 6, 1, 9, 0, tzinfo=UTC)

OVERDUE = "OVERDUE"
AT_RISK = "AT_RISK"


class ComputedRiskFlagsTestCase(TestCase):
    """A project whose due date has passed reads as overdue, with nothing having run."""

    def setUp(self) -> None:
        self.scenario = PortfolioScenario()

    def test_the_project_detail_reads_overdue_with_no_recompute_at_all(self) -> None:
        detail = read_project_detail(project_code=PROJECT_CODE, now=AFTER_THE_TARGET_DATE)

        self.assertIn(OVERDUE, {flag.code for flag in detail.risk_flags})
        self.assertEqual(detail.health.code, AT_RISK)
        self.assertIsNone(detail.score, "nothing scored this project, and it still reads correctly")
        self.assertFalse(
            PriorityScore.objects.exists(),
            "the flag is a function of the rows, not a by-product of the engine having run",
        )

    def test_a_snapshot_written_before_the_date_still_reads_overdue_after_it(self) -> None:
        rebuild_snapshot(project_code=PROJECT_CODE, now=BEFORE_THE_TARGET_DATE)

        page = read_queue(SnapshotQueueFilters(), now=AFTER_THE_TARGET_DATE)

        item = next(row for row in page.items if row.code == PROJECT_CODE)
        self.assertIn(OVERDUE, {flag.code for flag in item.risk_flags})
        self.assertEqual(item.health.code, AT_RISK)

    def test_the_same_snapshot_reads_as_it_did_on_the_day_it_was_written(self) -> None:
        """The reading follows ``now`` and nothing else — the row is identical in both tests."""
        rebuild_snapshot(project_code=PROJECT_CODE, now=BEFORE_THE_TARGET_DATE)

        page = read_queue(SnapshotQueueFilters(), now=BEFORE_THE_TARGET_DATE)

        item = next(row for row in page.items if row.code == PROJECT_CODE)
        self.assertNotIn(OVERDUE, {flag.code for flag in item.risk_flags})

    def test_the_queue_can_still_be_filtered_by_a_flag_no_column_holds(self) -> None:
        rebuild_snapshot(project_code=PROJECT_CODE, now=BEFORE_THE_TARGET_DATE)

        matched = read_queue(
            SnapshotQueueFilters(risk_flag_codes=(OVERDUE,)), now=AFTER_THE_TARGET_DATE
        )
        unmatched = read_queue(
            SnapshotQueueFilters(risk_flag_codes=(OVERDUE,)), now=BEFORE_THE_TARGET_DATE
        )

        self.assertEqual([row.code for row in matched.items], [PROJECT_CODE])
        self.assertEqual(matched.count, 1)
        self.assertEqual(unmatched.count, 0)


class NoStoredRiskStateTestCase(TestCase):
    """There is nowhere for a stale flag to be, which is the actual guarantee.

    Asserted against the schema rather than against a query, because "no row is wrong" is only
    interesting if there is no row at all: a column that still existed would be a column something
    could start writing again.
    """

    def test_no_table_stores_a_risk_flag(self) -> None:
        model_names = {model.__name__ for model in apps.get_models()}
        self.assertNotIn("RiskFlag", model_names)

    def test_the_read_model_holds_the_facts_and_none_of_the_conclusions(self) -> None:
        columns = {field.name for field in ProjectSnapshot._meta.fields}

        self.assertNotIn("risk_flags", columns)
        self.assertNotIn("health", columns)
        for fact in (
            "state_category",
            "target_date",
            "next_step",
            "overdue_task_count",
            "blocked_task_count",
            "in_progress_task_count",
            "open_blocker_count",
            "last_activity_at",
            "owner_load_points",
            "owner_capacity_points",
        ):
            with self.subTest(column=fact):
                self.assertIn(fact, columns)
