"""The inputs of the ``work`` use cases, as frozen Pydantic models.

A command object rather than a long keyword signature, for two reasons. Shape validation
(a non-empty title, a blocker kind inside the closed set, a timezone-aware ``now``) happens
once at construction, so a service body is invariants and effects only; and every command
carries ``actor``, ``correlation_id`` and ``now`` explicitly, which is what makes a replay
deterministic and lets "deprioritize A to prioritize B" be reconstructed as one movement.

Partial updates are expressed with Pydantic's ``model_fields_set`` rather than with a
sentinel: a field absent from the payload is left alone, a field present and ``None`` is
cleared. That distinction is why ``UpdateTaskCommand`` can unassign a task at all.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Final
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from apps.work.domain.value_objects import BlockerKind


class WorkCommand(BaseModel):
    """Fields every ``work`` use case needs to write its audit record and outbox row.

    ``now`` is domain time supplied by the caller, never read from the clock inside a
    service: that is what makes the same command produce the same ``ActivityRecord`` and
    ``OutboxEvent`` when replayed, and what lets a test assert on an exact instant.
    """

    model_config = ConfigDict(frozen=True)

    actor: str = Field(min_length=1, max_length=32)
    correlation_id: UUID
    now: datetime

    @field_validator("now")
    @classmethod
    def _reject_naive_datetime(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            message = "now must be timezone-aware; a naive instant is ambiguous across deploys."
            raise ValueError(message)
        return value


#: What a task code looks like: a project code, the task infix, and a number
#: (``PRJ-15-T02``). Mirrored from ``work.models.TASK_CODE_INFIX`` rather than
#: imported, because ``domain/`` stays free of Django (CLAUDE.md rule 6).
_TASK_CODE_PATTERN: Final = re.compile(r"^[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*-[Tt]\d+$")


class DependencySpec(BaseModel):
    """One prerequisite of a task being created, resolved or not.

    61 of the 82 source tasks name their dependency as free text that does not resolve to
    a task, so an unresolved spec is the normal case and is kept verbatim in ``raw_label``.
    Dropping it would delete the operation's own notes; the constraint mirrored here is
    ``depends_on_id IS NOT NULL OR raw_label <> ''`` from ``DATA_MODEL`` §9.2.
    """

    model_config = ConfigDict(frozen=True)

    depends_on_code: str | None = None
    raw_label: str = ""

    @classmethod
    def from_text(cls, text: str) -> DependencySpec:
        """Read one prerequisite the way an operator wrote it.

        The wire carries a single string per dependency, because that is what the
        source data is: a column in which most rows are a sentence and a few are a
        task code. Deciding which one it is belongs here rather than in the router
        — it is the rule that gives ``depends_on_code``/``raw_label`` their meaning,
        and a router that classified it would put a domain rule in the HTTP layer
        (ARCHITECTURE §7).

        A code is a reference to a task and therefore has the shape of one: no
        whitespace, ending in the task infix and a number. Anything else is the
        operation's own words and is kept verbatim, unresolved — which the class
        docstring above calls the normal case. Treating prose as a code is what
        makes ``POST /projects/{code}/tasks`` answer ``404`` for a dependency that
        was never meant to resolve.
        """
        label = text.strip()
        looks_like_code = (
            label != ""
            and not any(character.isspace() for character in label)
            and _TASK_CODE_PATTERN.match(label) is not None
        )
        if looks_like_code:
            return cls(depends_on_code=label.upper(), raw_label=label)
        return cls(raw_label=label)

    @model_validator(mode="after")
    def _reject_empty_dependency(self) -> DependencySpec:
        if self.depends_on_code is None and not self.raw_label.strip():
            message = "A dependency needs either a target task code or a raw label."
            raise ValueError(message)
        return self


class CreateTaskCommand(WorkCommand):
    """Add a task to a project, optionally with its prerequisites.

    The initial workflow state is not an input: it is resolved from the project's
    ``WorkflowBinding``, because a caller able to pick the starting state could enter a
    task straight into a terminal one without a declared transition.
    """

    project_code: str = Field(min_length=1, max_length=16)
    #: Left empty to let the service mint ``{project_code}-T{NN}``, which is what the HTTP API
    #: always does: `docs/API.md` §2.8 does not accept a code from a client, because a caller that
    #: chooses an identifier can collide with one an already-published event names. A fixture or a
    #: migration pins its own code by setting this.
    code: str = Field(default="", max_length=16)
    title: str = Field(min_length=1, max_length=200)
    priority_code: str = Field(min_length=1, max_length=32)
    assignee_code: str | None = None
    due_date: date | None = None
    detail: str = ""
    description: str = ""
    last_progress: str = Field(default="", max_length=255)
    dependencies: tuple[DependencySpec, ...] = ()


class UpdateTaskCommand(WorkCommand):
    """Edit the mutable fields of a task.

    ``workflow_state`` is deliberately absent: a state change goes through
    ``transition_task`` and the workflow app's transition service, never through a field
    assignment (``CLAUDE.md`` rule 2). Only the fields present in the payload are touched.

    ``is_archived`` carries both directions of a soft delete (ADR 0012), exactly as
    ``UpdateMemberCommand.is_active`` carries retire and restore. ``DELETE /tasks/{code}``
    builds this command with ``is_archived=True`` rather than reaching for a service of its
    own, so removing and putting back are one code path and cannot come to disagree about
    what else a removal touches.

    ``dependencies`` is the **whole** prerequisite set, not a delta: absent leaves the edges
    alone, and any tuple — the empty one included — replaces them. A set has no unambiguous
    patch spelling, which is the reasoning ``TransitionUpdateIn.requires_fields`` already
    states about its own list; a delta would additionally need "remove this one" to name an
    edge, and an edge that is still only prose has no identity to name it by.
    """

    task_code: str = Field(min_length=1, max_length=16)
    title: str = Field(default="", min_length=1, max_length=200)
    detail: str = ""
    description: str = ""
    last_progress: str = Field(default="", max_length=255)
    priority_code: str = Field(default="", min_length=1, max_length=32)
    assignee_code: str | None = None
    due_date: date | None = None
    is_archived: bool | None = None
    dependencies: tuple[DependencySpec, ...] | None = None


class TransitionTaskCommand(WorkCommand):
    """Move a task along a declared edge of its workflow.

    ``reason`` is free text; whether it is mandatory is a property of the edge
    (``WorkflowTransition.requires_reason``) and is enforced by the transition service, not
    here, so adding a reason requirement stays a data change.
    """

    task_code: str = Field(min_length=1, max_length=16)
    to_state_code: str = Field(min_length=1, max_length=32)
    reason: str = ""


class RaiseBlockerCommand(WorkCommand):
    """Record an impediment against a project, or against one task of it.

    ``project_code`` is always required even for a task-level blocker: the column is
    always populated (``DATA_MODEL`` §4) and the service cross-checks the pair rather than
    inferring silently, so a mismatched task and project is a rejected request instead of
    a blocker attached to the wrong portfolio row.
    """

    project_code: str = Field(min_length=1, max_length=16)
    task_code: str | None = None
    description: str = Field(min_length=1)
    kind: BlockerKind
    owner_code: str | None = None


class ResolveBlockerCommand(WorkCommand):
    """Close an open blocker, stating how it was cleared.

    ``reason`` is mandatory here and in the database. A blocker closed without a recorded
    resolution is indistinguishable from one that was never real, and it is how the same
    impediment comes back a month later with nobody able to say what was tried.
    """

    blocker_id: int = Field(gt=0)
    reason: str = Field(min_length=1, max_length=255)


class AddNoteCommand(WorkCommand):
    """Append a chronological comment to a project, or to one task of it.

    ``Note.author`` is written from ``actor`` and stored as a denormalized string, not a
    foreign key, so the comment survives its author leaving the roster.
    """

    project_code: str = Field(min_length=1, max_length=16)
    task_code: str | None = None
    body: str = Field(min_length=1)
