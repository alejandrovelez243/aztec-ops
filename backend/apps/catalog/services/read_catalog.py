"""Use case: serve the whole operator-editable vocabulary the frontend renders pickers from."""

from apps.catalog.domain.value_objects import CatalogView
from apps.catalog.models import Currency, EngagementType, Priority, ProjectType, Role, Stage


def read_catalog() -> CatalogView:
    """Read every active taxonomy value, in the order an operator arranged it.

    Six index scans on ``(is_active, order)`` and nothing else — the tables hold tens of rows, so
    the document is cheap enough to fetch on every page load and simple enough that the client
    never needs to cache it defensively.

    Only active rows are published: retirement is ``is_active = False`` and it exists precisely to
    take a value out of the pickers, while the projects already pointing at it keep resolving
    through their foreign key. Serving a retired row here would let an operator choose it again and
    quietly un-retire it.

    Returns:
        The vocabulary as an immutable :class:`~apps.catalog.domain.value_objects.CatalogView`,
        safe to serialize with no further database access.
    """
    return CatalogView(
        engagement_types=tuple(row.to_ref() for row in EngagementType.objects.active()),
        project_types=tuple(row.to_ref() for row in ProjectType.objects.active()),
        stages=tuple(row.to_ref() for row in Stage.objects.active()),
        priorities=tuple(row.to_ref() for row in Priority.objects.active()),
        roles=tuple(row.to_ref() for row in Role.objects.active()),
        currencies=tuple(row.to_currency_ref() for row in Currency.objects.active()),
    )
