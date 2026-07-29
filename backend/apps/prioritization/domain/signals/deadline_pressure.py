"""The ``deadline_pressure`` signal: how close the project is to its committed date."""

from typing import Final

from ..registry import register
from ..types import SignalInput, SignalResult

#: Distance at which a target date stops exerting any pressure. Sixty days is roughly a delivery
#: quarter: beyond it, a date is a plan rather than a constraint on today's work.
PRESSURE_HORIZON_DAYS: Final[int] = 60

#: Score used when no target date exists. Deliberately the midpoint: an undated project is not
#: safe (it cannot be shown to be far away) and not urgent (nothing was committed). The missing
#: date is surfaced as the NO_TARGET_DATE flag instead of being smuggled into the number.
NO_TARGET_DATE_SCORE: Final[float] = 0.5


@register("deadline_pressure")
class DeadlinePressure:
    """Days remaining until ``target_date``, normalized against a sixty-day horizon.

    Boundaries: a target date in the past returns 1.0 and stays there — an overdue project cannot
    become more overdue in a way that should outrank another overdue one on this signal alone. A
    null target date returns 0.5 and the project raises ``NO_TARGET_DATE``. A date at or beyond
    the horizon returns 0.0.
    """

    def evaluate(self, data: SignalInput) -> SignalResult:
        """Read the deadline pressure of one project.

        Args:
            data: The assembled facts, including ``now``; the strategy never reads a clock.

        Returns:
            The normalized pressure and a sentence naming the date and the day count.
        """
        if data.target_date is None:
            return SignalResult(
                score=NO_TARGET_DATE_SCORE,
                reason="No target date is committed, so the deadline cannot be evaluated.",
            )

        days_remaining = (data.target_date - data.now.date()).days
        if days_remaining < 0:
            return SignalResult(
                score=1.0,
                reason=f"Target date {data.target_date.isoformat()} is {-days_remaining} day(s) in the past.",
            )
        if days_remaining == 0:
            return SignalResult(
                score=1.0,
                reason=f"Target date {data.target_date.isoformat()} is today.",
            )

        score = 1.0 - days_remaining / PRESSURE_HORIZON_DAYS
        return SignalResult.clamped(
            round(score, 4),
            f"Target date {data.target_date.isoformat()} is {days_remaining} day(s) away.",
        )
