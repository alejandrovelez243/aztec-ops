"""Pure invariants of the project aggregate.

These duplicate two database check constraints (DATA_MODEL §9.2) on purpose. The constraint is the
authority — it also covers the admin and a raw SQL fix — but a violation reaching PostgreSQL
surfaces as an ``IntegrityError`` and therefore as a 500. Checking here turns the same violation
into a typed domain error the central handler maps to 422, with the offending values named.
"""

from datetime import date
from decimal import Decimal

from apps.portfolio.domain.errors import InvalidDateWindow, NegativeBusinessValue


def ensure_valid_date_window(start_date: date | None, target_date: date | None) -> None:
    """Reject a start date later than the target date.

    Either date may be absent and that absence is meaningful, not incomplete: 9 source projects
    have no start date and 5 have no target date, and a null target is the ``NO_TARGET_DATE`` risk
    signal. Only a fully specified, inverted pair is an error.

    Args:
        start_date: When delivery began, or None.
        target_date: The committed date, or None.

    Returns:
        None.

    Raises:
        InvalidDateWindow: Both dates are present and start is strictly after target.
    """
    if start_date is None or target_date is None:
        return
    if start_date <= target_date:
        return
    raise InvalidDateWindow(start_date.isoformat(), target_date.isoformat())


def ensure_non_negative_business_value(business_value: Decimal | None) -> None:
    """Reject a negative contract value.

    None is allowed: not every engagement in the source carries a value, and the prioritization
    engine treats the absence as its own case rather than as zero.

    Args:
        business_value: Contract value, or None.

    Returns:
        None.

    Raises:
        NegativeBusinessValue: The value is present and below zero.
    """
    if business_value is None or business_value >= 0:
        return
    raise NegativeBusinessValue(str(business_value))
