"""Payload schemas of the ``work`` topics that have one (EVENTS.md §4).

Pure: no Django, no model, no envelope. The envelope is assembled by ``apps.events.services``;
what lives here is only the body, so a payload can be constructed and asserted on
``SimpleTestCase``.

Only ``task.archive_changed`` is declared here so far. The older ``task.*`` topics still build
their payload as a literal inside their service, and this module is where they move when one of
them next changes shape — a payload with a class is a payload a consumer's test can import, and
this is the first ``work`` topic whose body carries a fact a subscriber has to *branch* on rather
than merely render.
"""

from pydantic import BaseModel, ConfigDict, Field


class TaskArchiveChangedPayload(BaseModel):
    """Body of ``task.archive_changed``.

    One topic for both directions, carrying ``is_archived``, rather than a ``task.deleted`` and a
    ``task.restored``. A subscriber's question is a boolean — "does this task still count?" — and
    answering it with a topic name would force every subscription to list both and treat them
    identically, which is the shape of the bug the first time somebody adds only one of them. The
    roster's ``member.activation_changed`` is the same decision for the same reason.

    ``project_code`` travels so a consumer aggregates without a foreign key into ``work``: the
    counts that move when a task is removed — open, overdue, blocked — are read per project, and
    the snapshot rebuild and the priority engine both address a project by code.

    Removal is a *soft* delete (ADR 0012). The task's row, its dependency edges, its notes and its
    blockers all survive, so a consumer must not read ``is_archived: true`` as "this code no longer
    exists": the code is never reused and the task can come back.
    """

    model_config = ConfigDict(frozen=True)

    project_code: str = Field(min_length=1, max_length=16)
    title: str = Field(min_length=1, max_length=200)
    is_archived: bool
