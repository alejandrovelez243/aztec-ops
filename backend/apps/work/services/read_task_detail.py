"""Use case: read one task in full, including the moves it may legally make.

The task counterpart of ``read_project_detail`` and deliberately built the same way. Served from
the write side rather than from any projection, so an operator who has just moved a task and lands
on its screen sees the move; and assembled in one response, so the screen never renders half of
itself while a second request is in flight.

``transitions`` is the load-bearing part, and it comes from ``WorkflowTransition`` — the same table
the transition service validates against. A task's legal moves are never a list held anywhere else:
if this read and ``transition_task`` could disagree, the UI would offer a button whose request is
refused.

**Two entry points, not one with a flag.** :func:`read_task_detail` serves ``GET`` and is scoped to
the tasks nobody removed, so a removed task is a 404 there (ADR 0012).
:func:`read_task_detail_after_write` serves the ``PATCH`` response and is not scoped, because the
caller that just moved ``is_archived`` is entitled to see the row it wrote — a 404 in reply to a
successful write is not an answer. A boolean parameter selecting between the two would be the
behaviour-selecting flag CLAUDE.md rule 13 forbids, and it would put the decision in the caller's
argument list instead of in the route.
"""

from datetime import datetime
from typing import Final

from apps.work.domain.errors import TaskNotFound
from apps.work.domain.views import TaskDetailView
from apps.work.models import Note, Task, TaskQuerySet
from apps.workflow.models import WorkflowTransition

#: How many of a task's comments the detail carries. A ceiling, not a page: a task accumulates a
#: handful of notes and the screen shows them all, so paging would cost every reader a second
#: request to learn there is no second page. The cap exists only so one pathological task cannot
#: turn its detail into an unbounded response — reaching it is the signal to add a paginated
#: ``/tasks/{code}/notes`` *beside* this field, never instead of it.
TASK_NOTE_LIMIT: Final = 100


def read_task_detail(*, task_code: str, now: datetime) -> TaskDetailView:
    """Assemble the task detail response for ``GET /api/v1/tasks/{code}``.

    Scoped to the tasks nobody removed. A removed task is a ``404 not_found`` here rather than a
    page rendered with a "removed" banner, and that is the same answer the transition route gives:
    a task that is out of the operation's attention has no screen and no legal moves (ADR 0012).
    Reaching it again is ``GET /projects/{code}/tasks?is_archived=true``, which is what the
    restore control is built on.

    Args:
        task_code: The business code (``PRJ-01-T02``), never a primary key.
        now: Domain time. One instant for the whole response, so ``is_overdue`` and every note's
            age are judged against the same clock — a screen whose parts disagreed about "now" is
            one nobody can reason about.

    Returns:
        The task with its project, state, owner, priority, dependencies, legal transitions and
        comments newest first.

    Raises:
        TaskNotFound: No task carries that code, or the task carrying it was removed. Raised
            rather than returning an empty shape, so a stale code from the UI is a 404 an operator
            can act on.
    """
    return _detail_of(Task.objects.active().for_code(task_code), task_code=task_code, now=now)


def read_task_detail_after_write(*, task_code: str, now: datetime) -> TaskDetailView:
    """The same response, unscoped, for the ``PATCH`` that just wrote the task.

    Not scoped by ``is_archived``, because the caller that just moved that field is entitled to
    see the row it wrote: answering a successful removal with a 404 would make the write look like
    it failed. Split from :func:`read_task_detail` rather than selected by a flag — the scope is a
    property of the route, not an argument a caller should be able to get wrong.

    Args:
        task_code: The business code of the task that was just written.
        now: Domain time, the same instant the write used.

    Returns:
        The task as it now stands, removed or not.

    Raises:
        TaskNotFound: No task carries that code at all.
    """
    return _detail_of(Task.objects.for_code(task_code), task_code=task_code, now=now)


def _detail_of(selection: TaskQuerySet, *, task_code: str, now: datetime) -> TaskDetailView:
    """Assemble the detail from an already-scoped selection of at most one task.

    The two public entry points differ only in that scope, so the joins, the transition read and
    the note limit are stated once here — a second copy is how the two would come to disagree
    about how many comments a detail carries.

    Raises:
        TaskNotFound: The selection is empty.
    """
    task = selection.with_relations().with_dependencies().first()
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
