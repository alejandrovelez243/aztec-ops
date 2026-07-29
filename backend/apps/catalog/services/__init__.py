"""Use cases of the catalog context.

One read and no writes: the taxonomies are edited in the admin, which is the whole reason they are
rows. A service that created a taxonomy value over HTTP would be a second, unaudited editor of the
vocabulary every other context compares against.
"""

from apps.catalog.services.read_catalog import read_catalog

__all__ = ["read_catalog"]
