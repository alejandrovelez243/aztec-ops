"""Persistence of the audit trail.

One table, ``activity_activityrecord``, and the two overrides that make "append-only" a
property of the code rather than a sentence in a document. Fields, ``Meta`` and ``__str__``
only: what a record *means* is decided by the service that writes it, and every query lives in
``repositories.py``.
"""

from typing import Any, ClassVar

from django.db import models
from django.utils import timezone

from apps.activity.domain.errors import (
    ActivityRecordCannotBeDeleted,
    ActivityRecordIsAppendOnly,
)
from apps.activity.domain.value_objects import ActivityEntry


class ActivityRecord(models.Model):
    """One immutable fact about a project, a task or a blocker.

    This is the answer to "when did this project change state, and what was deprioritized to
    make room for it". Rows are appended and never touched again: there is no ``updated_at``
    column, ``save()`` refuses to update an existing row and ``delete()`` refuses outright.

    The three vocabularies below are ``TextChoices`` because they are structural — adding a
    verb is a migration and a code change, unlike a workflow state or a taxonomy row, which the
    operation edits from the admin (CLAUDE.md rule 1).
    """

    class EntityType(models.TextChoices):
        """What kind of thing the record is about."""

        PROJECT = "project", "Project"
        TASK = "task", "Task"
        BLOCKER = "blocker", "Blocker"

    class Verb(models.TextChoices):
        """The closed set of facts the trail can state (DATA_MODEL §5)."""

        CREATED = "CREATED", "Created"
        STATE_CHANGED = "STATE_CHANGED", "State changed"
        PRIORITY_CHANGED = "PRIORITY_CHANGED", "Priority changed"
        BLOCKER_RAISED = "BLOCKER_RAISED", "Blocker raised"
        BLOCKER_RESOLVED = "BLOCKER_RESOLVED", "Blocker resolved"
        OWNER_CHANGED = "OWNER_CHANGED", "Owner changed"
        NEXT_STEP_SET = "NEXT_STEP_SET", "Next step set"
        TASK_ADDED = "TASK_ADDED", "Task added"
        NOTE_ADDED = "NOTE_ADDED", "Note added"
        SEEDED = "SEEDED", "Seeded"

    class Origin(models.TextChoices):
        """Who or what caused the fact, which is what makes a score movement arguable."""

        MANUAL = "MANUAL", "Manual"
        POLICY = "POLICY", "Policy"
        SYSTEM = "SYSTEM", "System"

    entity_type = models.CharField(max_length=16, choices=EntityType.choices)
    entity_id = models.CharField(max_length=32)
    verb = models.CharField(max_length=24, choices=Verb.choices)
    origin = models.CharField(max_length=8, choices=Origin.choices, default=Origin.SYSTEM)
    actor = models.CharField(max_length=32)
    from_value = models.CharField(max_length=255, blank=True, default="")
    to_value = models.CharField(max_length=255, blank=True, default="")
    reason = models.CharField(max_length=500, blank=True, default="")
    metadata = models.JSONField(default=dict, blank=True)
    occurred_at = models.DateTimeField(default=timezone.now)
    correlation_id = models.UUIDField()

    class Meta:
        """Ordering and the four indexes DATA_MODEL §10.2 names, each for a stated query."""

        ordering: ClassVar[list[str]] = ["-occurred_at", "-id"]
        verbose_name = "activity record"
        verbose_name_plural = "activity records"
        indexes: ClassVar[list[models.Index]] = [
            # The project timeline (DATA_MODEL §10.2): equality on the leading pair, sort on the
            # trailing column, so LIMIT 50 stops after 50 index entries however large this grows.
            models.Index(
                fields=["entity_type", "entity_id", "-occurred_at"],
                name="activity_entity_recent_idx",
            ),
            # Expands either half of "deprioritize A to prioritize B" into the whole decision.
            models.Index(fields=["correlation_id"], name="activity_correlation_idx"),
            models.Index(fields=["verb", "-occurred_at"], name="activity_verb_recent_idx"),
            models.Index(fields=["actor", "-occurred_at"], name="activity_actor_recent_idx"),
        ]

    def __str__(self) -> str:
        """Name the fact the way the admin's change list reads it."""
        return f"{self.entity_type}:{self.entity_id} {self.verb} by {self.actor}"

    def save(self, *args: Any, **kwargs: Any) -> None:
        """Insert the record, refusing any update of a row that already exists.

        An audit trail that can be edited is not an audit trail: if a state change can be
        rewritten after the fact, no reader can tell the record apart from the story someone
        preferred afterwards. Corrections are appended, which is why the table has no
        ``updated_at`` column to advertise a mutation path that must not exist.

        ``loaddata`` bypasses this method (it calls ``save_base(raw=True)``), so seeding stays
        possible while no application code can mutate a row.

        Raises:
            ActivityRecordIsAppendOnly: The instance already exists in the database.
        """
        if not self._state.adding:
            raise ActivityRecordIsAppendOnly(self.pk)
        super().save(*args, **kwargs)

    def delete(self, *_args: Any, **_kwargs: Any) -> tuple[int, dict[str, int]]:
        """Refuse the deletion, always.

        Same reason as ``save()``: a trail with a delete path cannot tell a reader what is
        missing from it. Retention, if it is ever needed, is a deliberate archival job run by a
        database administrator, not a call the application is allowed to make.

        Raises:
            ActivityRecordCannotBeDeleted: Always. There is no permitted caller.
        """
        raise ActivityRecordCannotBeDeleted(self.pk)

    def to_entry(self) -> ActivityEntry:
        """Describe this row as the frozen value the timeline reads render.

        Every read of the trail returns this projection rather than the instance itself:
        ``repositories.project_timeline`` for ``GET /api/v1/projects/{code}/timeline`` and
        ``repositories.decision_trail`` for the "why did this move" panel, which both serialize
        outside the transaction that queried them. Handing the model out instead would let the
        router resolve fields lazily after that transaction closed, and would tie the response
        shape to the column list.

        Nothing is omitted: the value object carries every column, ``id`` included, because a
        caller that wrote a record through ``write_activity`` and a caller that queried one must
        hold the same type. The row is append-only, so this projection can never go stale
        against its source.

        Returns:
            The record as an immutable :class:`ActivityEntry`. Issues no query — every field
            read here lives on this row.
        """
        return ActivityEntry(
            id=self.pk,
            entity_type=self.entity_type,
            entity_id=self.entity_id,
            verb=self.verb,
            origin=self.origin,
            actor=self.actor,
            from_value=self.from_value,
            to_value=self.to_value,
            reason=self.reason,
            metadata=self.metadata,
            occurred_at=self.occurred_at,
            correlation_id=self.correlation_id,
        )
