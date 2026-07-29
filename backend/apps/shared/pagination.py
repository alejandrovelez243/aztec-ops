"""The one paginated envelope every list endpoint returns (`docs/API.md` §1.3).

``{items, count}`` and nothing else: ``count`` is the total number of matching rows, not the length
of the page, and the client derives the page count from it. No ``next``/``previous`` URLs are sent,
because a client that follows an opaque link cannot express "page 4 of the same filter set" in its
own routing, which is exactly what the command center's URL has to hold.

Paging is applied in the read service rather than by ninja's ``@paginate`` decorator: the services
return frozen projections, so a decorator wrapping the route would have to materialise every row
before slicing it, and the ``count`` would then cost a second full read instead of one ``COUNT(*)``
against the same predicate.
"""

from pydantic import BaseModel, ConfigDict, Field

#: Rows returned when the caller names no page size, matching the LIMIT of DATA_MODEL §10.2.
DEFAULT_PAGE_SIZE = 50

#: The largest page the API will build. A caller asking for more is clamped rather than rejected:
#: an oversized ``page_size`` is a client bug that must not turn a working dashboard into a 422,
#: while an unbounded one is a client bug that would turn one request into a table scan.
MAX_PAGE_SIZE = 200


class Page[ItemT](BaseModel):
    """One window of a filtered list, plus how many rows the filter matched in total.

    Generic so every list endpoint declares its own item type in the OpenAPI schema; a
    ``Page[dict]`` would satisfy the envelope while telling the generated client nothing.
    """

    model_config = ConfigDict(frozen=True)

    items: tuple[ItemT, ...]
    count: int = Field(ge=0)


class PageWindow(BaseModel):
    """The offset and limit a read service applies, derived from ``page`` / ``page_size``.

    Built here rather than in each router so "page 1 is the first page" is decided once. A
    ``page_size`` above :data:`MAX_PAGE_SIZE` is clamped, never rejected — §1.3 fixes that as the
    contract, so the cap can be lowered later without breaking a client that asked for more.
    """

    model_config = ConfigDict(frozen=True)

    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE)

    @classmethod
    def of(cls, *, page: int, page_size: int) -> "PageWindow":
        """Translate a 1-based page request into a window.

        Args:
            page: 1-based page number. Values below 1 are raised to 1, because "page 0" is a
                client arithmetic slip and an empty result would hide it.
            page_size: Requested rows per page, clamped into ``[1, MAX_PAGE_SIZE]``.

        Returns:
            The window to hand to a read service.
        """
        safe_page = max(1, page)
        safe_size = min(MAX_PAGE_SIZE, max(1, page_size))
        return cls(offset=(safe_page - 1) * safe_size, limit=safe_size)
