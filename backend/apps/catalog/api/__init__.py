"""HTTP surface of the catalog context: one read of the whole vocabulary, and nothing else.

There is no write route. The taxonomies are operator data edited in the admin (ARCHITECTURE §3.1),
and an endpoint that created one would be a second editor of the vocabulary every other context
compares ``code`` against.
"""

from apps.catalog.api.routers import router

__all__ = ["router"]
