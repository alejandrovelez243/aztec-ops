"""The ``blockage`` signal: open impediments, weighted by how long they have been open."""

from typing import Final

from ..registry import register
from ..types import SignalInput, SignalResult

#: The floor a project reaches the moment a single blocker is open. Being blocked at all is
#: already half of this signal; age decides the rest.
OPEN_BLOCKER_FLOOR: Final[float] = 0.5

#: Age at which the oldest open blocker saturates the signal. Forty days is well past the point
#: where "it is being handled" has stopped being true.
AGE_SATURATION_DAYS: Final[int] = 40


@register("blockage")
class Blockage:
    """Open blockers, with the age of the oldest raising the score rather than lowering it.

    An older blocker scores **higher**, which is the opposite of a decay and is the point of the
    signal: a blocker open for three weeks is not "waiting", it is rotting. Decaying it would let
    the exact projects the command center exists to surface sink quietly out of the queue while
    the operation looks at fresher, less damaged work.

    Boundaries: no open blockers returns 0.0; one open blocker raised today returns 0.5; an oldest
    blocker at or past forty days returns 1.0.
    """

    # Spanish on purpose: ``label`` and every ``reason`` below are user-facing (CLAUDE.md §Language).
    label = "Bloqueo"

    def evaluate(self, data: SignalInput) -> SignalResult:
        """Read the blockage pressure of one project.

        Args:
            data: The assembled facts; openness is ``Blocker.resolved_at IS NULL`` resolved by the
                caller, never a substring match on the blocker's prose.

        Returns:
            The normalized pressure and a sentence naming the blocker count and the oldest age.
        """
        if data.open_blocker_count == 0:
            return SignalResult(score=0.0, reason="Sin bloqueos abiertos.")

        oldest_days = data.oldest_blocker_age_days or 0
        score = OPEN_BLOCKER_FLOOR + (1.0 - OPEN_BLOCKER_FLOOR) * (
            oldest_days / AGE_SATURATION_DAYS
        )
        return SignalResult.clamped(
            round(score, 4),
            f"{data.open_blocker_count} bloqueo(s) abierto(s); el más antiguo lleva "
            f"{oldest_days} día(s) sin resolverse y necesita intervención.",
        )
