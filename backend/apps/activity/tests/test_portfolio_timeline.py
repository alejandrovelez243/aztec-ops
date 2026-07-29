"""What ``read_portfolio_timeline`` returns: the order, the facets and the count.

The read is the whole point of the endpoint, so it is asserted here against the service rather
than only through HTTP: a facet that quietly matches nothing is a bug the router cannot show,
because an empty page is also a legitimate answer.

``TestCase`` and not ``SimpleTestCase``: every assertion here is about a predicate the database
evaluates, and a rolled-back transaction per test is what keeps six inserts cheap.
"""

from datetime import timedelta
from uuid import UUID

from django.test import TestCase

from apps.activity.models import ActivityRecord
from apps.activity.services import PortfolioTimelineFilters, read_portfolio_timeline
from apps.activity.tests.trail import (
    BLOCKED_AT,
    CAMILA,
    CREATED_AT,
    DARIO,
    DECISION_ID,
    MOVED_AT,
    OTHER_PROJECT_CODE,
    PRIORITY_AT,
    PROJECT_CODE,
    SYSTEM_ACTOR,
    TASK_ADDED_AT,
    TASK_CODE,
    Trail,
)


def _ids(filters: PortfolioTimelineFilters) -> tuple[int, ...]:
    """Read the feed and return the ids of the page, in the order it returned them."""
    return tuple(entry.id for entry in read_portfolio_timeline(filters).items)


class PortfolioTimelineOrderingTestCase(TestCase):
    """The feed is a narrative: newest fact first, and never two orderings for one input."""

    trail: Trail

    @classmethod
    def setUpTestData(cls) -> None:
        cls.trail = Trail()

    def test_the_unfiltered_feed_returns_every_fact_newest_first(self) -> None:
        page = read_portfolio_timeline(PortfolioTimelineFilters())

        self.assertEqual(tuple(entry.id for entry in page.items), self.trail.newest_first())
        self.assertEqual(page.count, 6)

    def test_two_facts_at_the_same_instant_are_ordered_by_the_later_insertion_first(self) -> None:
        # Both sides of one reprioritization carry the same occurred_at, so without the -id
        # tiebreaker their relative order would be whatever the planner happened to produce —
        # and a page boundary falling between them could then repeat or skip a row.
        page = read_portfolio_timeline(PortfolioTimelineFilters(since=PRIORITY_AT))

        self.assertEqual(
            tuple(entry.id for entry in page.items),
            (self.trail.lowered.id, self.trail.raised.id),
        )
        self.assertGreater(self.trail.lowered.id, self.trail.raised.id)

    def test_the_ordering_holds_across_pages_so_the_tied_pair_is_not_split_twice(self) -> None:
        pages = [_ids(PortfolioTimelineFilters(limit=1, offset=offset)) for offset in range(6)]

        self.assertEqual(tuple(page[0] for page in pages), self.trail.newest_first())


class PortfolioTimelineFacetTestCase(TestCase):
    """Every facet narrows, and the ones that OR do OR."""

    trail: Trail

    @classmethod
    def setUpTestData(cls) -> None:
        cls.trail = Trail()

    def test_entity_type_narrows_to_one_kind_of_subject(self) -> None:
        self.assertEqual(
            _ids(PortfolioTimelineFilters(entity_type=ActivityRecord.EntityType.BLOCKER)),
            (self.trail.blocker_raised.id,),
        )

    def test_entity_id_narrows_to_one_subject_across_every_verb_it_carries(self) -> None:
        self.assertEqual(
            _ids(PortfolioTimelineFilters(entity_id=PROJECT_CODE)),
            (self.trail.lowered.id, self.trail.moved.id, self.trail.created.id),
        )

    def test_entity_type_and_entity_id_together_select_exactly_one_entity(self) -> None:
        self.assertEqual(
            _ids(
                PortfolioTimelineFilters(
                    entity_type=ActivityRecord.EntityType.TASK, entity_id=TASK_CODE
                )
            ),
            (self.trail.task_added.id,),
        )

    def test_a_type_that_does_not_own_the_id_selects_nothing_rather_than_ignoring_one(
        self,
    ) -> None:
        page = read_portfolio_timeline(
            PortfolioTimelineFilters(
                entity_type=ActivityRecord.EntityType.TASK, entity_id=PROJECT_CODE
            )
        )

        self.assertEqual(page.items, ())
        self.assertEqual(page.count, 0)

    def test_a_single_verb_selects_only_the_records_stating_it(self) -> None:
        self.assertEqual(
            _ids(PortfolioTimelineFilters(verbs=(ActivityRecord.Verb.STATE_CHANGED,))),
            (self.trail.moved.id,),
        )

    def test_repeating_the_verb_ors_its_values_instead_of_intersecting_them(self) -> None:
        # The intersection of two verbs is empty by construction — one row states one verb — so
        # an AND here would silently turn the useful filter into a blank screen.
        self.assertEqual(
            _ids(
                PortfolioTimelineFilters(
                    verbs=(
                        ActivityRecord.Verb.STATE_CHANGED,
                        ActivityRecord.Verb.BLOCKER_RAISED,
                    )
                )
            ),
            (self.trail.blocker_raised.id, self.trail.moved.id),
        )

    def test_actor_selects_what_one_person_caused(self) -> None:
        self.assertEqual(
            _ids(PortfolioTimelineFilters(actor=DARIO)),
            (self.trail.blocker_raised.id, self.trail.task_added.id),
        )

    def test_the_engine_is_an_actor_like_any_other(self) -> None:
        self.assertEqual(
            _ids(PortfolioTimelineFilters(actor=SYSTEM_ACTOR)),
            (self.trail.lowered.id, self.trail.raised.id),
        )

    def test_origin_separates_the_engines_own_bookkeeping_from_the_rest(self) -> None:
        self.assertEqual(
            _ids(PortfolioTimelineFilters(origin=ActivityRecord.Origin.POLICY)),
            (self.trail.lowered.id, self.trail.raised.id),
        )
        self.assertEqual(
            _ids(PortfolioTimelineFilters(origin=ActivityRecord.Origin.MANUAL)),
            (self.trail.blocker_raised.id, self.trail.moved.id),
        )

    def test_correlation_id_expands_either_half_of_a_decision_into_the_whole_movement(
        self,
    ) -> None:
        page = read_portfolio_timeline(PortfolioTimelineFilters(correlation_id=DECISION_ID))

        self.assertEqual(
            tuple(entry.id for entry in page.items),
            (self.trail.lowered.id, self.trail.raised.id),
        )
        self.assertEqual(
            {entry.entity_id for entry in page.items}, {PROJECT_CODE, OTHER_PROJECT_CODE}
        )

    def test_the_facets_compose_into_one_predicate(self) -> None:
        page = read_portfolio_timeline(
            PortfolioTimelineFilters(
                verbs=(
                    ActivityRecord.Verb.PRIORITY_CHANGED,
                    ActivityRecord.Verb.BLOCKER_RAISED,
                ),
                origin=ActivityRecord.Origin.POLICY,
                since=BLOCKED_AT,
                until=PRIORITY_AT,
            )
        )

        self.assertEqual(
            tuple(entry.id for entry in page.items),
            (self.trail.lowered.id, self.trail.raised.id),
        )
        self.assertEqual(page.count, 2)


class PortfolioTimelineWindowTestCase(TestCase):
    """The time window is applied to ``occurred_at`` and both of its bounds are inclusive."""

    trail: Trail

    @classmethod
    def setUpTestData(cls) -> None:
        cls.trail = Trail()

    def test_since_includes_a_fact_that_happened_exactly_on_the_bound(self) -> None:
        self.assertIn(
            self.trail.blocker_raised.id, _ids(PortfolioTimelineFilters(since=BLOCKED_AT))
        )

    def test_until_includes_a_fact_that_happened_exactly_on_the_bound(self) -> None:
        self.assertIn(self.trail.created.id, _ids(PortfolioTimelineFilters(until=CREATED_AT)))

    def test_a_bound_one_microsecond_past_the_fact_excludes_it(self) -> None:
        # The pair with the previous two tests is what pins "inclusive" as the contract the
        # docstring states, rather than leaving it to whichever lookup was typed.
        self.assertNotIn(
            self.trail.blocker_raised.id,
            _ids(PortfolioTimelineFilters(since=BLOCKED_AT + timedelta(microseconds=1))),
        )
        self.assertNotIn(
            self.trail.created.id,
            _ids(PortfolioTimelineFilters(until=CREATED_AT - timedelta(microseconds=1))),
        )

    def test_both_bounds_together_select_the_closed_interval(self) -> None:
        page = read_portfolio_timeline(
            PortfolioTimelineFilters(since=MOVED_AT, until=TASK_ADDED_AT)
        )

        self.assertEqual(
            tuple(entry.id for entry in page.items),
            (self.trail.task_added.id, self.trail.moved.id),
        )
        self.assertEqual(page.count, 2)


class PortfolioTimelineUnknownFacetTestCase(TestCase):
    """A value this deployment does not know is a question, not a fault."""

    trail: Trail

    @classmethod
    def setUpTestData(cls) -> None:
        cls.trail = Trail()

    def test_an_unknown_facet_value_matches_nothing_instead_of_raising(self) -> None:
        cases = {
            "verb": PortfolioTimelineFilters(verbs=("RETIRED_VERB",)),
            "actor": PortfolioTimelineFilters(actor="nobody"),
            "entity_type": PortfolioTimelineFilters(entity_type="engagement"),
            "origin": PortfolioTimelineFilters(origin="ORACLE"),
            "entity_id": PortfolioTimelineFilters(entity_id="PRJ-NOPE"),
            "correlation_id": PortfolioTimelineFilters(
                correlation_id=UUID("99999999-9999-4999-8999-999999999999")
            ),
        }
        for facet, filters in cases.items():
            with self.subTest(facet=facet):
                page = read_portfolio_timeline(filters)

                self.assertEqual(page.items, ())
                self.assertEqual(page.count, 0)

    def test_one_unknown_verb_alongside_a_known_one_still_returns_the_known_records(self) -> None:
        page = read_portfolio_timeline(
            PortfolioTimelineFilters(verbs=("RETIRED_VERB", ActivityRecord.Verb.STATE_CHANGED))
        )

        self.assertEqual(tuple(entry.id for entry in page.items), (self.trail.moved.id,))


class PortfolioTimelineCrossContextTestCase(TestCase):
    """The feed spans contexts — that is what it is for."""

    trail: Trail

    @classmethod
    def setUpTestData(cls) -> None:
        cls.trail = Trail()

    def test_projects_tasks_and_blockers_all_appear_in_one_unfiltered_feed(self) -> None:
        page = read_portfolio_timeline(PortfolioTimelineFilters())

        self.assertEqual(
            {entry.entity_type for entry in page.items},
            {
                ActivityRecord.EntityType.PROJECT,
                ActivityRecord.EntityType.TASK,
                ActivityRecord.EntityType.BLOCKER,
            },
        )

    def test_facts_about_more_than_one_project_appear_side_by_side(self) -> None:
        page = read_portfolio_timeline(
            PortfolioTimelineFilters(verbs=(ActivityRecord.Verb.PRIORITY_CHANGED,))
        )

        self.assertEqual(
            {entry.entity_id for entry in page.items}, {PROJECT_CODE, OTHER_PROJECT_CODE}
        )


class PortfolioTimelinePagingTestCase(TestCase):
    """A window is a window: pages neither overlap nor skip, and ``count`` ignores them."""

    trail: Trail

    @classmethod
    def setUpTestData(cls) -> None:
        cls.trail = Trail()

    def test_consecutive_pages_partition_the_feed_without_overlap_or_gap(self) -> None:
        first = _ids(PortfolioTimelineFilters(limit=2, offset=0))
        second = _ids(PortfolioTimelineFilters(limit=2, offset=2))
        third = _ids(PortfolioTimelineFilters(limit=2, offset=4))

        self.assertEqual(first + second + third, self.trail.newest_first())
        self.assertEqual(set(first) & set(second), set())

    def test_count_is_the_total_matched_rows_and_not_the_length_of_the_page(self) -> None:
        page = read_portfolio_timeline(PortfolioTimelineFilters(limit=2))

        self.assertEqual(len(page.items), 2)
        self.assertEqual(page.count, 6)

    def test_count_follows_the_filters_rather_than_the_table(self) -> None:
        page = read_portfolio_timeline(PortfolioTimelineFilters(actor=CAMILA, limit=1))

        self.assertEqual(len(page.items), 1)
        self.assertEqual(page.count, 2)

    def test_an_offset_past_the_end_is_an_empty_page_that_still_reports_the_total(self) -> None:
        page = read_portfolio_timeline(PortfolioTimelineFilters(offset=100))

        self.assertEqual(page.items, ())
        self.assertEqual(page.count, 6)


class EmptyTrailTestCase(TestCase):
    """The first read of a fresh deployment is the empty page, never an error."""

    def test_an_empty_trail_reads_as_an_empty_page_with_a_zero_count(self) -> None:
        page = read_portfolio_timeline(PortfolioTimelineFilters())

        self.assertEqual(page.items, ())
        self.assertEqual(page.count, 0)
