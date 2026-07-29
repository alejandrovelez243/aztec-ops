"""What ``read_project_timeline`` returns, now that it composes the same queryset facets.

The read was rewritten onto ``with_verbs`` and ``occurred_between`` and its behaviour is claimed
unchanged. "Unchanged" is only a claim until something asserts it, so this file pins the four
properties the refactor could have moved: the project scope, the OR over verbs, the inclusive
window, and the fact that the feed never leaks a record about another project.
"""

from datetime import timedelta

from django.test import TestCase

from apps.activity.models import ActivityRecord
from apps.activity.services import TimelineFilters, read_project_timeline
from apps.activity.tests.trail import (
    BLOCKED_AT,
    CREATED_AT,
    DECISION_ID,
    MOVED_AT,
    OTHER_PROJECT_CODE,
    PRIORITY_AT,
    PROJECT_CODE,
    Trail,
)


class ProjectTimelineTestCase(TestCase):
    """One project's narrative, and only that project's."""

    trail: Trail

    @classmethod
    def setUpTestData(cls) -> None:
        cls.trail = Trail()

    def test_the_timeline_holds_only_project_records_of_the_named_project(self) -> None:
        page = read_project_timeline(TimelineFilters(project_code=PROJECT_CODE))

        self.assertEqual(
            tuple(entry.id for entry in page.items),
            (self.trail.lowered.id, self.trail.moved.id, self.trail.created.id),
        )
        self.assertEqual(page.count, 3)

    def test_a_task_of_the_project_is_not_a_record_of_the_project(self) -> None:
        # The task's entity_id starts with the project code, so a scope built on a prefix rather
        # than on the entity_type/entity_id pair would let it through.
        page = read_project_timeline(TimelineFilters(project_code=PROJECT_CODE))

        self.assertNotIn(self.trail.task_added.id, [entry.id for entry in page.items])
        self.assertEqual(
            {entry.entity_type for entry in page.items}, {ActivityRecord.EntityType.PROJECT}
        )

    def test_verbs_or_within_the_project_scope(self) -> None:
        page = read_project_timeline(
            TimelineFilters(
                project_code=PROJECT_CODE,
                verbs=(ActivityRecord.Verb.CREATED, ActivityRecord.Verb.STATE_CHANGED),
            )
        )

        self.assertEqual(
            tuple(entry.id for entry in page.items),
            (self.trail.moved.id, self.trail.created.id),
        )

    def test_no_verb_facet_leaves_the_selection_untouched(self) -> None:
        self.assertEqual(
            read_project_timeline(TimelineFilters(project_code=PROJECT_CODE, verbs=())).count, 3
        )

    def test_both_window_bounds_are_inclusive(self) -> None:
        inside = read_project_timeline(
            TimelineFilters(project_code=PROJECT_CODE, since=CREATED_AT, until=MOVED_AT)
        )
        outside = read_project_timeline(
            TimelineFilters(
                project_code=PROJECT_CODE,
                since=CREATED_AT + timedelta(microseconds=1),
                until=MOVED_AT - timedelta(microseconds=1),
            )
        )

        self.assertEqual(
            tuple(entry.id for entry in inside.items),
            (self.trail.moved.id, self.trail.created.id),
        )
        self.assertEqual(outside.items, ())

    def test_a_correlation_id_stays_confined_to_the_project_being_read(self) -> None:
        page = read_project_timeline(
            TimelineFilters(project_code=PROJECT_CODE, correlation_id=DECISION_ID)
        )

        self.assertEqual(tuple(entry.id for entry in page.items), (self.trail.lowered.id,))
        self.assertNotIn(OTHER_PROJECT_CODE, [entry.entity_id for entry in page.items])

    def test_paging_reports_the_matched_total_rather_than_the_page_length(self) -> None:
        page = read_project_timeline(TimelineFilters(project_code=PROJECT_CODE, limit=1))

        self.assertEqual(len(page.items), 1)
        self.assertEqual(page.items[0].id, self.trail.lowered.id)
        self.assertEqual(page.count, 3)

    def test_a_project_code_matching_nothing_is_an_empty_page_and_not_an_error(self) -> None:
        page = read_project_timeline(TimelineFilters(project_code="PRJ-NOPE"))

        self.assertEqual(page.items, ())
        self.assertEqual(page.count, 0)

    def test_the_tie_between_two_instants_is_broken_the_same_way_as_in_the_feed(self) -> None:
        page = read_project_timeline(TimelineFilters(project_code=PROJECT_CODE, since=PRIORITY_AT))

        self.assertEqual(tuple(entry.id for entry in page.items), (self.trail.lowered.id,))
        self.assertEqual(
            read_project_timeline(
                TimelineFilters(project_code=OTHER_PROJECT_CODE, since=BLOCKED_AT)
            ).count,
            1,
        )
