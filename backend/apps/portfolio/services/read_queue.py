"""Use case: read one page of the prioritized project queue.

The command center's single read (`docs/API.md` §2.1). It is served entirely from
``ProjectSnapshot`` — the denormalized read model of ARCHITECTURE §8 — and never from the write
aggregates: resolving project + score + risk flags + owner load + task counts through the ORM on
every request is the join this read model exists to avoid.

Two queries, always, whatever the filters: one ``COUNT(*)`` over the whole match so the client can
build a pager, and one windowed scan for the page itself. The count is not derived from the page
length, because the page length only ever answers "is there at least this much".
"""

from apps.portfolio.domain.value_objects import SnapshotQueueFilters
from apps.portfolio.domain.views import QueueItemView
from apps.portfolio.models import ProjectSnapshot
from apps.shared.pagination import Page


def read_queue(filters: SnapshotQueueFilters) -> Page[QueueItemView]:
    """Return one window of the queue, plus how many projects the filters matched in total.

    Ordering is stabilised by ``project_code`` inside the queryset, so paging cannot repeat or
    drop a project when two of them tie on the requested field.

    Args:
        filters: The requested facets, ordering and window. An unfiltered read is
            ``SnapshotQueueFilters()``.

    Returns:
        The page and the total match count.

    Raises:
        UnknownOrdering: ``filters.order_by`` names a field outside the allowlist.
    """
    matched = ProjectSnapshot.objects.matching(filters)
    return Page(
        items=(
            matched.in_requested_order(filters.order_by)
            .page(offset=filters.offset, limit=filters.limit)
            .as_queue_items()
        ),
        count=matched.count(),
    )
