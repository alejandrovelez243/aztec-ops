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

from datetime import date, datetime
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
    last_progress: str = Field(default="", max_length=255)
    dependencies: tuple[DependencySpec, ...] = ()


class UpdateTaskCommand(WorkCommand):
    """Edit the mutable fields of a task.

    ``workflow_state`` is deliberately absent: a state change goes through
    ``transition_task`` and the workflow app's transition service, never through a field
    assignment (``CLAUDE.md`` rule 2). Only the fields present in the payload are touched.
    """

    task_code: str = Field(min_length=1, max_length=16)
    title: str = Field(default="", min_length=1, max_length=200)
    detail: str = ""
    last_progress: str = Field(default="", max_length=255)
    priority_code: str = Field(default="", min_length=1, max_length=32)
    assignee_code: str | None = None
    due_date: date | None = None


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
