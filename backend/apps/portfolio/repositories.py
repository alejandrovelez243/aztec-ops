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

from collections.abc import Sequence

from django.db.models import Count, Q

from apps.accounts.models import User
from apps.portfolio.domain.value_objects import OwnerLoad
from apps.work.models import CLOSED_CATEGORIES, Task
from apps.workflow.models import StateCategory

#: Counts for a member the aggregate query returned no row for: they carry nothing, which is an
#: answer, not a gap.
_NO_TASKS: dict[str, int] = {
    "open_task_count": 0,
    "blocked_task_count": 0,
    "urgent_open_task_count": 0,
}


def owner_load_for_codes(member_codes: Sequence[str]) -> dict[str, OwnerLoad]:
    """Derive current load for the given people in one aggregate query.

    "Open" is ``workflow_state.category NOT IN (DONE, CANCELLED)`` and "urgent" is
    ``priority.is_urgent`` — both structural facts read from the taxonomy, so adding a workflow
    state or renaming a priority from the admin does not touch this query (DATA_MODEL §12).

    People with no tasks at all are still present in the result with zero counts: a person carrying
    nothing is an answer to "who is overloaded?", and dropping them would make the caller treat
    "no load" as "unknown person".

    Args:
        member_codes: ``accounts.User.code`` values to evaluate. Empty returns ``{}`` without
            querying.

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

    # "Open" is the complement of ``work``'s own closed vocabulary, imported rather than re-listed:
    # a second copy is how a sixth category would silently come to mean "open" (DATA_MODEL §12).
    is_open = ~Q(workflow_state__category__in=CLOSED_CATEGORIES)
    rows = (
        Task.objects.filter(assignee__code__in=capacities.keys())
        .values("assignee__code")
        .annotate(
            open_task_count=Count("pk", filter=is_open),
            blocked_task_count=Count(
                "pk", filter=Q(workflow_state__category=StateCategory.BLOCKED)
            ),
            urgent_open_task_count=Count("pk", filter=is_open & Q(priority__is_urgent=True)),
        )
    )
    counted: dict[str, dict[str, int]] = {
        str(row["assignee__code"]): {
            "open_task_count": int(row["open_task_count"]),
            "blocked_task_count": int(row["blocked_task_count"]),
            "urgent_open_task_count": int(row["urgent_open_task_count"]),
        }
        for row in rows
    }

    return {
        code: OwnerLoad(
            member_code=code,
            open_task_count=counted.get(code, _NO_TASKS)["open_task_count"],
            blocked_task_count=counted.get(code, _NO_TASKS)["blocked_task_count"],
            urgent_open_task_count=counted.get(code, _NO_TASKS)["urgent_open_task_count"],
            weekly_capacity_points=capacity,
        )
        for code, capacity in capacities.items()
    }
