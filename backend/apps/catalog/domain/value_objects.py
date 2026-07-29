"""Immutable shapes the catalog publishes: one currency, and the whole vocabulary at once.

Pure by construction — no Django import — so the shapes can be built and asserted on without a
database (``ARCHITECTURE`` §7). They are Pydantic models rather than dataclasses (CLAUDE.md rule
10), which is what lets the router return them directly as its response schema instead of restating
the same fields in an ``*Out`` class.
"""

from pydantic import BaseModel, ConfigDict, Field

from apps.shared.refs import TaxonomyRef

#: Widest exponent ISO-4217 defines. Restated here rather than imported from ``models`` because
#: ``domain/`` may not import persistence; the database constraint and this bound are checked
#: against each other by ``CurrencyRefTests``.
MAX_MINOR_UNITS = 4


class CurrencyRef(TaxonomyRef):
    """One currency, ready to render *and* to format an amount with.

    ``TaxonomyRef`` plus the one fact a client cannot derive: how many decimal places the amount
    carries. Extending the shared reference rather than defining a parallel one keeps a currency
    renderable by the same chip component as every other taxonomy — a client that only wants
    ``{code, label, color}`` reads exactly those and ignores the rest.

    Attributes:
        minor_units: ISO-4217 exponent — 0 for CLP and JPY, 2 for USD and EUR. Formatting 28000
            without it is wrong by two orders of magnitude for a zero-decimal currency.
    """

    model_config = ConfigDict(frozen=True)

    minor_units: int = Field(ge=0, le=MAX_MINOR_UNITS)


class CatalogView(BaseModel):
    """Every taxonomy the frontend renders a picker for, in one response.

    One document rather than six endpoints because the client needs all of them before it can draw
    a single form, and six round trips to draw one form is six chances to render half a page. Each
    list holds only the active rows, in the operator's own ``order``: a retired value keeps
    resolving on the projects that already point at it, but must not appear in a picker where
    somebody could choose it again.

    An empty list is a legitimate answer — a taxonomy nobody has seeded yet — and the client
    renders an empty select rather than treating it as a failure.
    """

    model_config = ConfigDict(frozen=True)

    engagement_types: tuple[TaxonomyRef, ...] = ()
    project_types: tuple[TaxonomyRef, ...] = ()
    stages: tuple[TaxonomyRef, ...] = ()
    priorities: tuple[TaxonomyRef, ...] = ()
    roles: tuple[TaxonomyRef, ...] = ()
    currencies: tuple[CurrencyRef, ...] = ()
