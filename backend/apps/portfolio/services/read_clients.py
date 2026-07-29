"""Use case: serve the counterparties a project can be registered against."""

from apps.portfolio.domain.views import ClientDirectoryView
from apps.portfolio.models import Client


def read_clients() -> ClientDirectoryView:
    """Read every active counterparty, in the alias order an operator scans.

    This lives in ``portfolio`` and not in ``catalog`` even though it feeds the same kind of picker:
    ``Client`` is a portfolio aggregate, and publishing it from ``GET /api/v1/catalog`` would make
    the catalog context read a model it does not own (ARCHITECTURE §3.3). The catalog is the
    operator-editable *vocabulary*; a counterparty is a party.

    Only active rows are published, for the reason ``read_catalog`` gives: retirement exists to
    take a value out of the pickers, so serving a retired one would let somebody choose it again.

    Returns:
        The directory as an immutable view, safe to serialize with no further database access.
    """
    return ClientDirectoryView(items=tuple(row.to_ref() for row in Client.objects.active()))
