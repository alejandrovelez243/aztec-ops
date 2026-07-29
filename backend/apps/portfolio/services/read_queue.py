"""Use case: read one page of the prioritized project queue.

The command center's single read (`docs/API.md` §2.1). It is served from ``ProjectSnapshot`` — the
denormalized read model of ARCHITECTURE §8 — and never from the write aggregates: resolving
project + score + owner load + task counts through the ORM on every request is the join this read
model exists to avoid.

**Risk flags and health are computed here, not stored** (ADR 0011). The snapshot carries the facts
they are derived from, and the specifications run in Python over the rows already loaded — one pass
over a page, no query per project. That is why this function needs a ``now``: overdue is
``due_date < today`` and stale is ``last_activity < now - N days``, so the answer belongs to the
instant it is asked at, not to the instant some handler last rebuilt a row.

The consequence lands on the two derived facets. ``health`` and ``risk_flag`` cannot be ``WHERE``
clauses any more, so they are applied after evaluation, in Python — see :func:`_derived_facets`
for the cost and why it is the right one.
"""

from datetime import datetime

from django.conf import settings

from apps.portfolio.domain.value_objects import SnapshotQueueFilters
from apps.portfolio.domain.views import QueueItemView
from apps.portfolio.models import ProjectSnapshot
from apps.shared.pagination import Page


def read_queue(filters: SnapshotQueueFilters, *, now: datetime) -> Page[QueueItemView]:
    """Return one window of the queue, plus how many projects the filters matched in total.

    Ordering is stabilised by ``project_code`` inside the queryset, so paging cannot repeat or
    drop a project when two of them tie on the requested field.

    Two paths, and the branch is the honest cost of deriving flags rather than storing them:

    * No derived facet requested — the common read. PostgreSQL counts and windows, exactly as
      before, and only the page's rows are evaluated.
    * ``health`` or ``risk_flag`` requested. The ordered match is materialised, evaluated and then
      filtered and windowed in Python, because the predicate is a function of six specifications
      and the only way to push it into SQL would be to write them a second time as ``WHERE``
      clauses — two implementations of "blocked" that agree until the day they do not.

    Both paths order by stored columns, so a project sits at the same position either way.

    Args:
        filters: The requested facets, ordering and window. An unfiltered read is
            ``SnapshotQueueFilters()``.
        now: The instant every row's flags are evaluated at. One instant for the whole page, so
            two projects cannot be judged against two different days.

    Returns:
        The page and the total match count.

    Raises:
        UnknownOrdering: ``filters.order_by`` names a field outside the allowlist.
    """
    threshold = int(settings.STALENESS_THRESHOLD_DAYS)
    matched = ProjectSnapshot.objects.matching(filters).in_requested_order(filters.order_by)

    if not _derived_facets(filters):
        return Page(
            items=(
                matched.page(offset=filters.offset, limit=filters.limit).as_queue_items(
                    now=now, staleness_threshold_days=threshold
                )
            ),
            count=matched.count(),
        )

    evaluated = matched.as_queue_items(now=now, staleness_threshold_days=threshold)
    selected = [item for item in evaluated if _matches_derived(item, filters)]
    return Page(
        items=tuple(selected[filters.offset : filters.offset + filters.limit]),
        count=len(selected),
    )


def _derived_facets(filters: SnapshotQueueFilters) -> bool:
    """Whether the caller filtered on something no column holds.

    Exactly ``health`` and ``risk_flag``. Naming them in one predicate rather than repeating the
    two conditions is what keeps the fast path and the evaluated path from disagreeing about which
    reads are cheap.
    """
    return filters.health is not None or bool(filters.risk_flag_codes)


def _matches_derived(item: QueueItemView, filters: SnapshotQueueFilters) -> bool:
    """Whether one evaluated row satisfies the derived facets.

    ``risk_flag_codes`` ANDs, which is the same rule the JSONB containment used to express: "blocked
    **and** overdue" is the question an operator asks, and the OR of two common flags is most of the
    portfolio. An unknown code simply matches nothing, exactly as an unknown owner does.
    """
    if filters.health is not None and item.health.code != filters.health:
        return False
    raised = {flag.code for flag in item.risk_flags}
    return all(code in raised for code in filters.risk_flag_codes)
