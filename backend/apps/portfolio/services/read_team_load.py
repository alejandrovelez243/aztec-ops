"""Use case: who is carrying how much, right now.

Computed from ``work.Task`` rows at read time and never stored. The source ``Team`` sheet ships its
own counters and they are deliberately not imported (ARCHITECTURE §10): a stored count and the
tasks it summarises drift apart silently, and the roster is exactly where nobody would notice.

The query itself is :func:`apps.portfolio.repositories.owner_load_for_codes` — the one query in
this codebase that belongs to no model, because its numerator is an aggregate over ``work.Task``
and its denominator a column of ``accounts.User``. "Who is overloaded?" is a portfolio question,
so it lives here rather than in either context it reads.
"""

from collections.abc import Sequence
from datetime import date

from apps.accounts.models import User
from apps.portfolio.domain.views import TeamLoadView
from apps.portfolio.repositories import owner_load_for_codes


def read_team_load(
    *,
    owner_codes: Sequence[str] = (),
    include_inactive: bool = False,
    as_of: date,
) -> tuple[TeamLoadView, ...]:
    """Return one row per person, ordered by the display name.

    Not paginated: the roster is five people, and a pager over five rows is ceremony that costs the
    client a second request to learn there is no second page.

    Args:
        owner_codes: Restrict to these people. Empty means the whole roster.
        include_inactive: Whether people retired from the operation are included. Excluded by
            default — ``is_active`` means "still part of the operation", so a retired person's load
            is history, not a staffing constraint.
        as_of: The date lateness is measured against, supplied by the caller so every row of one
            response is judged against the same day.

    Returns:
        The load of each matching person. A person carrying nothing is present with zeros, because
        "carries nothing" is an answer to the question and absence would read as "unknown".
    """
    roster = User.objects.with_role()
    if not include_inactive:
        roster = roster.active()
    if owner_codes:
        roster = roster.by_codes(owner_codes)

    people = list(roster)
    load = owner_load_for_codes([person.code for person in people], as_of=as_of)

    return tuple(
        TeamLoadView(
            alias=person.code,
            label=person.alias,
            role=person.role.label if person.role else None,
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
    )
