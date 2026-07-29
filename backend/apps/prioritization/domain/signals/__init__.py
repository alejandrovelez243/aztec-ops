"""The six signal strategies of ``ARCHITECTURE`` §4.1.

Importing this package is what populates the signal registry, so it is imported once by
``domain.scoring``. A signal that is never imported is a signal the policy loader will report as
missing rather than one that silently contributes zero.
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
