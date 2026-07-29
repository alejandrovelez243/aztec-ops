"""The ``criticality`` signal: how much urgent work is open on the project."""

from typing import Final

from ..registry import register
from ..types import SignalInput, SignalResult

#: Number of open urgent tasks at which this signal saturates. Past five, the project is already
#: at the top of this dimension and the difference between six and nine is not a ranking decision.
URGENT_SATURATION_TASKS: Final[int] = 5


@register("criticality")
class Criticality:
    """Volume of open tasks whose priority is marked urgent.

    "Urgent" is ``Priority.is_urgent`` on the catalog row, resolved by the caller — never a
    comparison against the Spanish labels ``Critica`` / ``Alta``, which the operation may rename
    from the admin. Boundaries: no urgent open tasks returns 0.0; five or more returns 1.0.
    """

    def evaluate(self, data: SignalInput) -> SignalResult:
        """Read the volume of urgent open work.

        Args:
            data: The assembled facts, with ``urgent_open_task_count`` already resolved through
                the ``is_urgent`` column.

        Returns:
            The normalized volume and a sentence naming the count.
        """
        if data.urgent_open_task_count == 0:
            return SignalResult(score=0.0, reason="No open tasks at an urgent priority.")

        score = data.urgent_open_task_count / URGENT_SATURATION_TASKS
        return SignalResult.clamped(
            round(score, 4),
            f"{data.urgent_open_task_count} open task(s) at an urgent priority.",
        )
