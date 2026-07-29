"""What the ``work`` context publishes to a read surface: tasks, blockers and notes.

The shapes `docs/API.md` §2.2, §2.10 and §2.12 fix on the wire. Pure Pydantic over the shared
kernel, no Django, so a router or a test builds one without a database.

Two fields are derived at read time and never stored, and both say so below. ``Task.is_overdue``
is ``due_date < today`` evaluated against server time, not the source spreadsheet's ``Si``/``No``
column, which was already stale the day it was exported. ``Blocker.age_days`` is measured from
``raised_at`` against the same instant, so two blockers rendered in one response cannot be aged
against two different clocks.

:class:`TaskDetailView` composes :class:`~apps.workflow.domain.views.TransitionOption`, owned by
the workflow context. That is an import between pure, Django-free ``domain/`` modules and nothing
more — a published-language dependency, exactly the one
:class:`~apps.portfolio.domain.views.ProjectDetailView` already takes (ARCHITECTURE §7). Restating
the option's three fields here would be a second spelling of the same contract, and the two would
drift the first time an edge grew a field.
"""

from datetime import date, datetime

from pydantic import BaseModel, ConfigDict, Field

from apps.shared.refs import ActorRef, StateRef, TaxonomyRef
from apps.workflow.domain.views import TransitionOption, WorkflowRef


class DependencyRef(BaseModel):
    """One prerequisite of a task, resolved to a task or kept as the operation's own words.

    61 of the 82 source tasks name their dependency as free text that resolves to no task, so an
    unresolved reference is the normal case rather than a defect. ``task_code`` is ``None`` then,
    and ``raw_label`` still carries what somebody actually wrote — dropping it would delete the
    operation's notes in the name of a clean schema.
    """

    model_config = ConfigDict(frozen=True)

    task_code: str | None = None
    raw_label: str = ""


class TaskView(BaseModel):
    """One task, as the project detail and the task list render it.

    ``state`` carries its ``category`` so the board can group by "blocked" without holding a list
    of state codes that two workflows could both own (DATA_MODEL §12).

    ``is_archived`` is ``False`` on every row of an ordinary list, because the list is scoped to
    the unremoved tasks. It travels anyway, and defaults to ``False`` rather than being omitted,
    so the one screen that asks for removed tasks renders them as removed instead of inferring it
    from the filter it happened to send (ADR 0012).
    """

    model_config = ConfigDict(frozen=True)

    code: str
    title: str
    assignee: ActorRef | None = None
    priority: TaxonomyRef
    state: StateRef
    due_date: date | None = None
    is_overdue: bool = False
    last_progress: str = ""
    is_archived: bool = False
    dependencies: tuple[DependencyRef, ...] = ()


class BlockerView(BaseModel):
    """One impediment, open or cleared.

    ``id`` is the numeric primary key and is contract only as a value to hand straight back to
    ``POST /api/v1/blockers/{id}/resolve`` (`docs/API.md` §4.2); it is not stable across a reseed.
    ``code`` is the business identifier the ``blocker.raised`` envelope carries, and is what a
    consumer or an audit reader addresses the row by.

    ``resolved_at`` being ``None`` *is* the definition of open — there is no separate boolean,
    because two representations of one fact is how the panel and the blockage signal come to
    disagree.
    """

    model_config = ConfigDict(frozen=True)

    id: int
    code: str
    kind: str
    description: str
    owner: ActorRef | None = None
    raised_at: datetime
    resolved_at: datetime | None = None
    resolution_reason: str = ""
    age_days: int = Field(default=0, ge=0)
    task_code: str | None = None


class NoteView(BaseModel):
    """One chronological comment on a project, or on one task of it.

    ``author`` is a bare code rather than an :class:`~apps.shared.refs.ActorRef` because the column
    is a denormalized string that outlives its author's row and can hold ``system``: resolving it
    to a person would fail for exactly the rows the denormalization exists to keep readable.

    Nothing parses a note's text. A note is not a substitute for a :class:`BlockerView`, and the
    moment something greps it for "blocked" the typed row stops being written.
    """

    model_config = ConfigDict(frozen=True)

    id: int
    code: str
    body: str
    author: str
    created_at: datetime
    task_code: str | None = None


class TaskProjectRef(BaseModel):
    """The project a task belongs to, as the task detail names it.

    Two fields and no more: ``code`` is what every other route is addressed by and ``name`` is what
    a header renders. Not a :class:`~apps.shared.refs.TaxonomyRef` — a project is an aggregate, not
    an operator-editable catalog row, and giving it a ``color`` would invite a client to paint a
    swatch for something the taxonomy does not own.

    Deliberately not the whole :class:`~apps.portfolio.domain.views.ProjectDetailView`: a task
    detail that embedded its project would carry that project's tasks, and therefore itself.
    """

    model_config = ConfigDict(frozen=True)

    code: str
    name: str


class TaskDetailView(BaseModel):
    """One task in full, as ``GET /api/v1/tasks/{code}`` returns it.

    The screen-shaped read: everything the task detail draws itself from arrives in one response.
    ``notes`` is inside rather than behind a second route for the reason the catalog read already
    states — a surface that needs two requests to render is two chances to render half a page, and
    the second request is the one that fails while the first has already painted. It is also the
    only shape in which the notes are guaranteed to describe *this* version of the task: two reads
    can straddle a write, and a comment rendered beside a state it does not refer to is worse than
    a slow page. A task carries a handful of comments, so the join costs nothing that would justify
    splitting it. Should a task ever carry hundreds, the fix is a paginated
    ``/tasks/{code}/notes`` *beside* this field, not instead of it — the first screenful must not
    become a second round trip.

    ``transitions`` is the **only** source of state buttons on this screen, and it comes from the
    same place the project detail's does: ``WorkflowTransition``, filtered to the active edges
    leaving the task's current state. The frontend holds no list of task state codes and never
    guesses legality, so adding a task state is a fixture row and zero frontend changes. An empty
    tuple is a legitimate answer — the task sits in a terminal state — and the UI renders no
    buttons rather than inventing one.

    ``is_overdue`` is derived from ``due_date`` against the instant the request was served, never
    read from a column, for the same reason it is on :class:`TaskView`.

    ``is_archived`` is always ``False`` on the ``GET`` route — a removed task is a 404 there
    (ADR 0012) — and is the point of the field on the ``PATCH`` response, which reports the task as
    it now stands whichever way the removal flag was just moved.

    ``workflow`` names the graph ``state`` and ``transitions`` both come from, and whether an ops
    lead chose it for this task. Two tasks of the same project can legitimately follow different
    lifecycles, so a screen showing only the state would leave an operator unable to say why the
    buttons differ from the task beside it.
    """

    model_config = ConfigDict(frozen=True)

    code: str
    title: str
    detail: str = ""
    description: str = ""
    project: TaskProjectRef
    assignee: ActorRef | None = None
    priority: TaxonomyRef
    state: StateRef
    workflow: WorkflowRef
    due_date: date | None = None
    is_overdue: bool = False
    last_progress: str = ""
    is_archived: bool = False
    dependencies: tuple[DependencyRef, ...] = ()
    transitions: tuple[TransitionOption, ...] = ()
    notes: tuple[NoteView, ...] = ()
    created_at: datetime
    updated_at: datetime
