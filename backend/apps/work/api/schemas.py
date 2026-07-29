"""Request schemas of the ``work`` endpoints — one per use case.

``workflow_state`` appears in none of them. A task starts in the ``is_initial`` state of the task
workflow and moves only through ``POST /api/v1/tasks/{code}/transition`` (CLAUDE.md rule 2); a
schema that accepted it would let a caller drop a task straight into a terminal state with no
declared edge and no audit record of how it got there.

``code`` appears in none of the creation schemas either: the service allocates
``{project_code}-T{NN}``, because a client-chosen identifier can collide with one an
already-published event names, and an event cannot be un-published.
"""

from datetime import date

from ninja import Field, Schema

from apps.shared.pagination import DEFAULT_PAGE_SIZE
from apps.work.domain.value_objects import BlockerKind
from apps.work.services.read_tasks import DEFAULT_TASK_ORDERING


class TaskCreateIn(Schema):
    """Body of ``POST /api/v1/projects/{code}/tasks``.

    ``depends_on`` names task codes inside the same project. A code that resolves to no task is
    kept verbatim as the dependency's ``raw_label`` rather than rejected: 61 of the 82 source tasks
    describe their prerequisite in prose, so an unresolvable reference is the normal case and
    dropping it would delete the operation's own notes.
    """

    title: str = Field(min_length=1, max_length=200)
    priority: str = Field(min_length=1, max_length=32)
    detail: str = ""
    description: str = ""
    assignee: str | None = None
    due_date: date | None = None
    last_progress: str = Field(default="", max_length=255)
    depends_on: list[str] = Field(default_factory=list)


class TaskUpdateIn(Schema):
    """Body of ``PATCH /api/v1/tasks/{code}``.

    Every field is optional and "absent" and "``null``" mean different things — the same contract
    ``ProjectUpdateIn`` documents. An absent field is left untouched; an explicit ``null`` clears
    the column, which is how a task is unassigned or loses its due date. The router reads which
    fields actually arrived from Pydantic's ``model_fields_set`` and carries that set into the
    command, so the distinction survives to the service. With a plain dict, "unassign this task"
    and "do not touch the assignee" would be the same request.

    ``null`` is therefore accepted only where the column is nullable. ``title``, ``detail``,
    ``last_progress`` and ``priority`` are ``NOT NULL``, so sending ``null`` for one of them is a
    422 rather than a silent clear: the value that would clear ``detail`` is ``""``, and it is
    accepted. ``title`` and ``priority`` additionally refuse ``""`` — a task with no title is not
    an edit anybody meant to make.

    ``workflow_state`` is absent, as it is from every schema in this module: a state moves through
    ``POST /api/v1/tasks/{code}/transition`` and nowhere else (CLAUDE.md rule 2).

    ``is_archived`` is the restore half of the soft delete (ADR 0012), the same way
    ``MemberUpdateIn.is_active`` is the restore half of retiring a person: ``DELETE`` removes and
    ``PATCH {"is_archived": false}`` puts back. Both directions are accepted here — a client that
    could only ever set it one way would need a second endpoint to undo an undo — and the response
    is the task as it now stands, removed or not.

    ``depends_on`` is sent **whole** and replaces the entire prerequisite set, ``[]`` included,
    which is how a task stops waiting on anything. That is the same decision
    ``workflow.TransitionUpdateIn.requires_fields`` documents — patching one entry of a list has no
    unambiguous spelling — and here it is stronger still: an edge that is only prose has no
    identifier a delta could name it by. Absent leaves the edges alone; ``null`` is accepted and
    means the same as absent, since ``[]`` already says "clear". Each entry is read exactly as at
    creation: a task code of this project resolves to an edge, anything else is kept verbatim as
    ``raw_label``.
    """

    title: str = Field(default="", min_length=1, max_length=200)
    detail: str = ""
    description: str = ""
    last_progress: str = Field(default="", max_length=255)
    priority: str = Field(default="", min_length=1, max_length=32)
    assignee: str | None = None
    due_date: date | None = None
    is_archived: bool | None = None
    depends_on: list[str] | None = None


class TaskTransitionIn(Schema):
    """Body of ``POST /api/v1/tasks/{code}/transition``.

    Identical in shape and in error contract to the project transition, against the task workflow.
    ``reason`` is optional here and mandatory when the edge sets ``requires_reason`` — a row an
    operator edits, so freezing the requirement into the schema would freeze a data decision.
    """

    to_state: str = Field(min_length=1, max_length=32)
    reason: str = ""


class BlockerCreateIn(Schema):
    """Body of ``POST /api/v1/projects/{code}/blockers``.

    ``task_code`` attaches the impediment to one task instead of to the project. The project is
    recorded either way — the service copies it from the task — so every consumer can resolve a
    blocker to a portfolio row without joining through ``Task``.
    """

    kind: BlockerKind
    description: str = Field(min_length=1)
    owner: str | None = None
    task_code: str | None = None


class BlockerResolveIn(Schema):
    """Body of ``POST /api/v1/blockers/{id}/resolve``.

    ``resolution`` is required and non-empty, here, in the service and as a database check. A
    blocker closed without saying how is indistinguishable from one that was never real, and it is
    how the same impediment returns a month later with nobody able to say what was tried.
    """

    resolution: str = Field(min_length=1, max_length=255)


class NoteIn(Schema):
    """Body of ``POST /api/v1/projects/{code}/notes``.

    Notes are chronological comments and nothing parses their text. A note is not a substitute for
    a :class:`~apps.work.domain.views.BlockerView`: the moment something greps one for "blocked",
    the typed row stops being written and the panel stops being true.
    """

    body: str = Field(min_length=1)
    task_code: str | None = None


class TaskQuery(Schema):
    """Query parameters of ``GET /api/v1/projects/{code}/tasks``.

    ``is_overdue`` is a derived facet: it compares ``due_date`` against server time inside the
    query, so it can never disagree with the ``is_overdue`` rendered on the rows it returns. The
    source spreadsheet's own ``Si``/``No`` column is not imported and is not part of this contract.

    ``is_archived`` is the **scope**, not a facet, and it is the same non-optional boolean
    defaulting to ``False`` that ``QueueQuery`` uses: removed tasks are out of the operation's
    attention by definition (ADR 0012), so the two sets are never mixed in one list. ``true`` is
    what the restore screen asks for — without it a removed task would be unreachable from the
    product and ``PATCH {"is_archived": false}`` would have no caller.
    """

    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=DEFAULT_PAGE_SIZE, ge=1)
    is_archived: bool = False
    assignee: list[str] = Field(default_factory=list)
    priority: list[str] = Field(default_factory=list)
    state: str | None = None
    state_category: str | None = None
    is_overdue: bool | None = None
    q: str = ""
    order_by: str = DEFAULT_TASK_ORDERING
