"""The ``business_value`` signal: contract value, compressed onto a logarithmic scale."""

from decimal import Decimal
from math import log1p
from typing import Final

from ..registry import register
from ..types import SignalInput, SignalResult

#: Reference used when the portfolio maximum is unknown or non-positive, so a single project can
#: still be scored in isolation (a recompute of one row must not depend on a portfolio-wide scan
#: succeeding).
FALLBACK_REFERENCE_VALUE: Final[Decimal] = Decimal("50000")

#: Score for a project with no recorded contract value. Zero, not the midpoint: an unknown value
#: is not evidence of importance, and inventing one would let a missing field outrank a small but
#: real contract.
UNKNOWN_VALUE_SCORE: Final[float] = 0.0


@register("business_value")
class BusinessValue:
    """Contract value normalized as ``log1p(value) / log1p(portfolio_maximum)``.

    Logarithmic on purpose. A 28k engagement does not deserve 3.5x the operational attention of an
    8k one; under linear normalization the two or three largest contracts pin the top of the queue
    permanently and every other signal becomes noise. The log compresses the tail so value orders
    projects without dominating them — 28k against a 40k portfolio maximum reads ~0.97 of the way
    up the scale, not 0.7 of it, and the gap between 8k and 28k stays visible without being
    decisive.

    Boundaries: a null or zero value returns 0.0; a value at or above the portfolio maximum
    returns 1.0.
    """

    def evaluate(self, data: SignalInput) -> SignalResult:
        """Read the normalized contract value.

        Args:
            data: The assembled facts; ``portfolio_max_business_value`` is the largest value in
                the active portfolio, supplied by the caller.

        Returns:
            The normalized value and a sentence naming the amount, its currency and the reference.
        """
        if data.business_value is None:
            return SignalResult(
                score=UNKNOWN_VALUE_SCORE,
                reason="No contract value is recorded, so value cannot raise this project.",
            )
        if data.business_value <= 0:
            return SignalResult(
                score=UNKNOWN_VALUE_SCORE,
                reason=f"Contract value is {data.business_value} {data.currency}.",
            )

        reference = data.portfolio_max_business_value or FALLBACK_REFERENCE_VALUE
        if reference <= 0:
            reference = FALLBACK_REFERENCE_VALUE

        score = log1p(float(data.business_value)) / log1p(float(reference))
        return SignalResult.clamped(
            round(score, 4),
            f"Contract value {data.business_value} {data.currency}, log-normalized against the "
            f"portfolio maximum of {reference}.",
        )
