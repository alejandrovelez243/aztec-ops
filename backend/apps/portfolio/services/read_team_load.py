"""Use case: who is carrying how much, right now.

Computed from ``work.Task`` rows at read time and never stored. The source ``Team`` sheet ships its
own counters and they are deliberately not imported (ARCHITECTURE §10): a stored count and the
tasks it summarises drift apart silently, and the roster is exactly where nobody would notice.

The query itself is :func:`apps.portfolio.repositories.owner_load_for_codes` — the one query in
this codebase that belongs to no model, because its numerator is an aggregate over ``work.Task``
and its denominator a column of ``accounts.User``. "Who is overloaded?" is a portfolio question,
so it lives here rather than in either context it reads.

The facets split across the two halves for the same reason. Everything that is a column of a person
— the search term, the role, the active flag — narrows the queryset before the aggregate runs, so
the database does the work. ``overloaded`` and the ordering are applied afterwards, in Python,
because utilization is derived here and does not exist for the database to filter or sort on.
"""

from collections.abc import Callable
from datetime import date
from operator import attrgetter

from apps.accounts.models import User
from apps.portfolio.domain.value_objects import ROSTER_ORDERING, RosterFilters
from apps.portfolio.domain.views import TeamLoadView
from apps.portfolio.repositories import owner_load_for_codes
from apps.shared.ordering import DESCENDING_PREFIX, UnknownOrdering

#: What a roster sort key can be. Every allowlisted field is a number or a string, and the two are
#: never mixed within one sort, so ``sorted`` never has to compare across the union.
type SortKey = str | int | float


def read_team_load(filters: RosterFilters, *, as_of: date) -> tuple[TeamLoadView, ...]:
    """Return one row per matching person, in the requested order.

    Args:
        filters: Which people to include and how to order them. The default is the active roster,
            most loaded first.
        as_of: The date lateness is measured against, supplied by the caller so every row of one
            response is judged against the same day.

    Returns:
        The load of each matching person. A person carrying nothing is present with zeros, because
        "carries nothing" is an answer to the question and absence would read as "unknown".

    Raises:
        UnknownOrdering: ``order_by`` names a field outside :data:`ROSTER_ORDERING`. A 422 naming
            the allowlist, never a silently different order than the one the client asked for.
    """
    key, descending = _ordering_key(filters.order_by)

    roster = User.objects.with_role().matching(filters.search).with_role_in(filters.role_codes)
    if filters.status == "active":
        roster = roster.active()
    elif filters.status == "inactive":
        roster = roster.inactive()
    if filters.owner_codes:
        roster = roster.by_codes(filters.owner_codes)

    people = list(roster)
    load = owner_load_for_codes([person.code for person in people], as_of=as_of)

    rows = [
        TeamLoadView(
            alias=person.code,
            label=person.alias,
            role=person.role.label if person.role else None,
            role_code=person.role.code if person.role else None,
            is_active=person.is_active,
            has_password=person.has_usable_password(),
            weekly_capacity_points=entry.weekly_capacity_points,
            load_points=entry.load_points,
            # Rounded to two decimals here rather than in the client: a utilization rendered as
            # 1.5499999999999998 in one browser and 1.55 in another is the same number failing to
            # look like one.
            utilization=round(entry.load_points / entry.weekly_capacity_points, 2),
            is_overloaded=entry.is_overloaded,
            open_tasks=entry.open_task_count,
            blocked_tasks=entry.blocked_task_count,
            high_or_critical_open=entry.urgent_open_task_count,
            overdue_tasks=entry.overdue_task_count,
            projects_owned=entry.owned_project_count,
        )
        for person in people
        if (entry := load.get(person.code)) is not None
    ]

    if filters.overloaded is not None:
        rows = [row for row in rows if row.is_overloaded is filters.overloaded]

    # ``label`` breaks every tie. Without it two people on identical load have no defined relative
    # order, and the roster would shuffle between two reads of the same unchanged data.
    rows.sort(key=attrgetter("label"))
    rows.sort(key=key, reverse=descending)
    return tuple(rows)


def _ordering_key(order_by: str) -> tuple[Callable[[TeamLoadView], SortKey], bool]:
    """Translate one signed wire field name into the sort key to apply and the direction.

    Args:
        order_by: A key of :data:`ROSTER_ORDERING`, optionally prefixed with ``-``.

    Returns:
        The function producing the sort key, and whether the order is descending.

    Raises:
        UnknownOrdering: The field is outside the allowlist. Carries the allowlist, so the client
            is told what it *may* sort by rather than only what it may not.
    """
    descending = order_by.startswith(DESCENDING_PREFIX)
    field_name = order_by.removeprefix(DESCENDING_PREFIX)
    attribute = ROSTER_ORDERING.get(field_name)
    if attribute is None:
        raise UnknownOrdering(field_name, tuple(ROSTER_ORDERING))
    if attribute == "role":
        # ``role`` is nullable and ``None`` does not compare against a string. Unclassified people
        # sort together under the empty string rather than raising, which is what an operator
        # grouping the roster by role expects to see.
        return (lambda row: row.role or ""), descending
    return attrgetter(attribute), descending
