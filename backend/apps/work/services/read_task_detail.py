"""Use case: read one task in full, including the moves it may legally make.

The task counterpart of ``read_project_detail`` and deliberately built the same way. Served from
the write side rather than from any projection, so an operator who has just moved a task and lands
on its screen sees the move; and assembled in one response, so the screen never renders half of
itself while a second request is in flight.

``transitions`` is the load-bearing part, and it comes from ``WorkflowTransition`` — the same table
the transition service validates against. A task's legal moves are never a list held anywhere else:
if this read and ``transition_task`` could disagree, the UI would offer a button whose request is
refused.
"""

from datetime import datetime
from typing import Final

from apps.work.domain.errors import TaskNotFound
from apps.work.domain.views import TaskDetailView
from apps.work.models import Note, Task
from apps.workflow.models import WorkflowTransition

#: How many of a task's comments the detail carries. A ceiling, not a page: a task accumulates a
#: handful of notes and the screen shows them all, so paging would cost every reader a second
#: request to learn there is no second page. The cap exists only so one pathological task cannot
#: turn its detail into an unbounded response — reaching it is the signal to add a paginated
#: ``/tasks/{code}/notes`` *beside* this field, never instead of it.
TASK_NOTE_LIMIT: Final = 100


def read_task_detail(*, task_code: str, now: datetime) -> TaskDetailView:
    """Assemble the task detail response.

    Args:
        task_code: The business code (``PRJ-01-T02``), never a primary key.
        now: Domain time. One instant for the whole response, so ``is_overdue`` and every note's
            age are judged against the same clock — a screen whose parts disagreed about "now" is
            one nobody can reason about.

    Returns:
        The task with its project, state, owner, priority, dependencies, legal transitions and
        comments newest first.

    Raises:
        TaskNotFound: No task carries that code. Raised rather than returning an empty shape, so a
            stale code from the UI is a 404 an operator can act on.
    """
    task = Task.objects.with_relations().with_dependencies().for_code(task_code).first()
    if task is None:
        raise TaskNotFound(task_code)

    return task.to_detail_view(
        today=now.date(),
        transitions=(
            WorkflowTransition.objects.active()
            .from_state(task.workflow_state_id)
            .with_states()
            .as_options()
        ),
        notes=Note.objects.for_task(task.pk).recent(TASK_NOTE_LIMIT).as_views(),
    )
