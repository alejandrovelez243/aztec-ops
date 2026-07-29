"""The ``staleness`` signal: time since anything was recorded, plus the absence of a next step."""

from typing import Final

from ..registry import register
from ..types import SignalInput, SignalResult

#: Added when the project has no ``next_step`` recorded. Silence is worse when nobody has written
#: down what happens next, but it is a nudge and not the whole signal: a project can be silent for
#: a good reason and still have a plan.
NO_NEXT_STEP_PENALTY: Final[float] = 0.25

#: Score for a project that has never recorded any activity at all. Full: there is no evidence
#: anyone has touched it, which is the strongest form of the fact this signal measures.
NEVER_ACTIVE_SCORE: Final[float] = 1.0


@register("staleness")
class Staleness:
    """Days without a recorded ``ActivityRecord``, relative to the configured threshold.

    The threshold arrives on the input (``STALENESS_THRESHOLD_DAYS``, default 14) rather than
    being frozen here, because how long silence is acceptable is an operational judgement.

    Boundaries: activity today returns 0.0 with a next step set, 0.25 without one; silence at or
    beyond the threshold returns 1.0; a project with no activity ever returns 1.0.
    """

    def evaluate(self, data: SignalInput) -> SignalResult:
        """Read how stale one project is.

        Args:
            data: The assembled facts; ``days_since_last_activity`` is ``None`` when no
                ``ActivityRecord`` exists for the project.

        Returns:
            The normalized staleness and a sentence naming the silence and the next step.
        """
        next_step_clause = (
            "next step is set" if data.next_step.strip() else "no next step is recorded"
        )
        penalty = 0.0 if data.next_step.strip() else NO_NEXT_STEP_PENALTY

        if data.days_since_last_activity is None:
            return SignalResult(
                score=NEVER_ACTIVE_SCORE,
                reason=f"No activity has ever been recorded; {next_step_clause}.",
            )

        silence_ratio = data.days_since_last_activity / data.staleness_threshold_days
        return SignalResult.clamped(
            round(silence_ratio + penalty, 4),
            f"No recorded activity for {data.days_since_last_activity} day(s) against a "
            f"{data.staleness_threshold_days}-day threshold; {next_step_clause}.",
        )
