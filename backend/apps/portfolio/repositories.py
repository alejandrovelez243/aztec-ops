"""The one query in this codebase that belongs to no model: owner load.

Every other named query lives on its model's ``QuerySet`` and is reached through ``objects`` —
``Project.objects.with_relations().by_code(code)``, ``ProjectSnapshot.objects.in_attention()`` —
because a query written as a free function returns a materialized list and therefore cannot be
narrowed, so every new combination needs a new function. This module is the documented exception,
and it is the *only* one. **Do not tidy this function onto a manager.** Doing so is what creates
the coupling it exists to prevent:

* It has two owners and no home. The numerator is an aggregate over ``work.Task`` rows keyed by
  assignee; the denominator, ``weekly_capacity_points``, is a column of ``accounts.User``. There is
  no single model whose manager could carry it without learning about the other context.
* On ``accounts`` it would teach identity that ``work.Task`` exists — the dependency the merge of
  ``TeamMember`` into ``User`` was careful not to introduce. On ``work`` it would put "who is
  overloaded?", a portfolio question, inside the context that merely owns the rows.
* Carrying the counts on the person instead would remove the query and reintroduce the stale
  projection the source ``Team`` sheet already is (DATA_MODEL §3).

So it lives in the context that *consumes* the answer. ``portfolio`` is allowed to know both
halves; neither half is allowed to know the other. Nothing here writes into ``work`` or
``accounts``.
"""

from collections.abc import Iterable, Sequence
from datetime import date

from django.db.models import Count, Q

from apps.accounts.models import User
from apps.portfolio.domain.value_objects import OwnerLoad
from apps.portfolio.models import Project
from apps.work.models import CLOSED_CATEGORIES, Task
from apps.workflow.models import StateCategory

#: Counts for a member the aggregate query returned no row for: they carry nothing, which is an
#: answer, not a gap.
_NO_TASKS: dict[str, int] = {
    "open_task_count": 0,
    "blocked_task_count": 0,
    "urgent_open_task_count": 0,
    "overdue_task_count": 0,
}


def owner_load_for_codes(
    member_codes: Sequence[str], *, as_of: date | None = None
) -> dict[str, OwnerLoad]:
    """Derive current load for the given people in three aggregate queries.

    "Open" is ``workflow_state.category NOT IN (DONE, CANCELLED)`` and "urgent" is
    ``priority.is_urgent`` — both structural facts read from the taxonomy, so adding a workflow
    state or renaming a priority from the admin does not touch this query (DATA_MODEL §12).

    People with no tasks at all are still present in the result with zero counts: a person carrying
    nothing is an answer to "who is overloaded?", and dropping them would make the caller treat
    "no load" as "unknown person".

    Args:
        member_codes: ``accounts.User.code`` values to evaluate. Empty returns ``{}`` without
            querying.
        as_of: The date lateness is measured against. ``None`` means the caller does not need the
            overdue count and it is reported as zero — the snapshot rebuild is such a caller,
            because it reads overdue work per *project*, not per person. Passed in rather than
            read from a clock so a replay reproduces the same answer.

    Returns:
        Load per person code. Codes that match nobody are absent from the mapping.
    """
    if not member_codes:
        return {}

    capacities = {
        code: user.weekly_capacity_points
        for code, user in User.objects.by_codes(member_codes).keyed_by_code().items()
    }
    if not capacities:
        return {}

    counted = _task_counts(codes=capacities.keys(), as_of=as_of)
    owned = _owned_project_counts(codes=capacities.keys())

    return {
        code: OwnerLoad(
            member_code=code,
            open_task_count=counted.get(code, _NO_TASKS)["open_task_count"],
            blocked_task_count=counted.get(code, _NO_TASKS)["blocked_task_count"],
            urgent_open_task_count=counted.get(code, _NO_TASKS)["urgent_open_task_count"],
            overdue_task_count=counted.get(code, _NO_TASKS)["overdue_task_count"],
            owned_project_count=owned.get(code, 0),
            weekly_capacity_points=capacity,
        )
        for code, capacity in capacities.items()
    }


def _task_counts(*, codes: Iterable[str], as_of: date | None) -> dict[str, dict[str, int]]:
    """Group ``work.Task`` by assignee into the four counts the roster reads.

    One grouped query rather than four, because the four definitions have to agree: counting
    "overdue" separately from "open" is how a cancelled task past its date starts inflating
    somebody's backlog forever.
    """
    # "Open" is the complement of ``work``'s own closed vocabulary, imported rather than re-listed:
    # a second copy is how a sixth category would silently come to mean "open" (DATA_MODEL §12).
    is_open = ~Q(workflow_state__category__in=CLOSED_CATEGORIES)
    # An absent ``as_of`` must count nothing, not everything: ``Q(pk__in=[])`` is the empty filter,
    # whereas omitting the predicate would count every open task as overdue.
    is_overdue = Q(due_date__lt=as_of) if as_of is not None else Q(pk__in=[])
    rows = (
        Task.objects.filter(assignee__code__in=codes)
        .values("assignee__code")
        .annotate(
            open_task_count=Count("pk", filter=is_open),
            blocked_task_count=Count(
                "pk", filter=Q(workflow_state__category=StateCategory.BLOCKED)
            ),
            urgent_open_task_count=Count("pk", filter=is_open & Q(priority__is_urgent=True)),
            overdue_task_count=Count("pk", filter=is_open & is_overdue),
        )
    )
    return {
        str(row["assignee__code"]): {
            "open_task_count": int(row["open_task_count"]),
            "blocked_task_count": int(row["blocked_task_count"]),
            "urgent_open_task_count": int(row["urgent_open_task_count"]),
            "overdue_task_count": int(row["overdue_task_count"]),
        }
        for row in rows
    }


def _owned_project_counts(*, codes: Iterable[str]) -> dict[str, int]:
    """Group unarchived ``portfolio.Project`` rows by owner.

    Archived projects are excluded: they are out of the operation's attention by definition, so
    counting them would make somebody look loaded by work nobody intends to do.
    """
    rows = (
        Project.objects.active()
        .filter(owner__code__in=codes)
        .values("owner__code")
        .annotate(owned=Count("pk"))
    )
    return {str(row["owner__code"]): int(row["owned"]) for row in rows}
