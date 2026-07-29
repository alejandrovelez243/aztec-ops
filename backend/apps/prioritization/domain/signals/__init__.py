"""The six signal strategies of ``ARCHITECTURE`` §4.1.

Importing this package is what populates the signal registry, so it is imported once by
``domain.scoring``. A signal that is never imported is a signal the policy loader will report as
missing rather than one that silently contributes zero.

Every ``label`` and every ``reason`` these six strategies produce is **Spanish on purpose**: the
interface is Spanish (PRODUCT.md) and the frontend is forbidden a table keyed by signal code
(`docs/standards/FRONTEND.md`), so the wording has to arrive from here. That is the same exception
CLAUDE.md §Language already grants the user-facing labels stored in the database — identifiers,
class names, constants, docstrings, comments and tests stay English.
"""

from . import (
    blockage,
    business_value,
    criticality,
    deadline_pressure,
    overdue_work,
    staleness,
)

__all__ = [
    "blockage",
    "business_value",
    "criticality",
    "deadline_pressure",
    "overdue_work",
    "staleness",
]
