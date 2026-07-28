"""What the ``work`` context publishes to a read surface: tasks, blockers and notes.

The shapes `docs/API.md` §2.2, §2.10 and §2.12 fix on the wire. Pure Pydantic over the shared
kernel, no Django, so a router or a test builds one without a database.

Two fields are derived at read time and never stored, and both say so below. ``Task.is_overdue``
is ``due_date < today`` evaluated against server time, not the source spreadsheet's ``Si``/``No``
column, which was already stale the day it was exported. ``Blocker.age_days`` is measured from
``raised_at`` against the same instant, so two blockers rendered in one response cannot be aged
against two different clocks.
"""

from datetime import date, datetime

from pydantic import BaseModel, ConfigDict, Field

from apps.shared.refs import ActorRef, StateRef, TaxonomyRef


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
