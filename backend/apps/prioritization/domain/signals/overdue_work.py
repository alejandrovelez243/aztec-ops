"""The ``overdue_work`` signal: how much of the open work has already slipped."""

from ..registry import register
from ..types import SignalInput, SignalResult


@register("overdue_work")
class OverdueWork:
    """Ratio of overdue tasks to open tasks.

    A ratio rather than a count, so a five-task engagement with four overdue tasks outranks a
    forty-task programme with five. Boundaries: no open tasks returns 0.0 (there is nothing to be
    late on, and an empty project should not inherit the previous reading), every open task
    overdue returns 1.0.
    """

    # Spanish on purpose: ``label`` and every ``reason`` below are user-facing (CLAUDE.md §Language).
    label = "Trabajo vencido"

    def evaluate(self, data: SignalInput) -> SignalResult:
        """Read the overdue proportion of one project's open work.

        Args:
            data: The assembled facts; ``overdue_task_count`` is derived from ``due_date`` against
                ``now`` by the caller, never from the source spreadsheet's stale flag.

        Returns:
            The normalized ratio and a sentence naming both counts.
        """
        if data.open_task_count == 0:
            return SignalResult(
                score=0.0, reason="Sin tareas abiertas, no hay nada que pueda estar vencido."
            )

        ratio = data.overdue_task_count / data.open_task_count
        return SignalResult.clamped(
            round(ratio, 4),
            f"{data.overdue_task_count} de {data.open_task_count} tareas abiertas pasaron su fecha.",
        )
