"""Persistence of the audit trail.

One table, ``activity_activityrecord``, and the two overrides that make "append-only" a
property of the code rather than a sentence in a document. Fields, ``Meta``, ``__str__`` and the
named reads of the trail: what a record *means* is still decided by the service that writes it.
"""

from collections.abc import Sequence
from datetime import datetime
from typing import Any, ClassVar
from uuid import UUID

from django.db import models
from django.utils import timezone

from apps.activity.domain.errors import (
    ActivityRecordCannotBeDeleted,
    ActivityRecordIsAppendOnly,
)
from apps.activity.domain.value_objects import TIMELINE_PAGE_SIZE, ActivityEntry


class ActivityRecordQuerySet(models.QuerySet["ActivityRecord"]):
    """The named reads of the audit trail.

    Every selection here composes; only :meth:`as_entries` and :meth:`recent` end the chain, and
    both say so. The questions the product asks — "what happened to this project", "what else
    moved as part of this decision", "what has the portfolio done this week" — are the same table
    read through different predicates, which is why they are methods on one queryset rather than
    a function per combination.

    Each facet is spelled as its own predicate and each returns ``self`` unchanged when the facet
    is absent, so a read service states the question and never the four-way branch around it.
    """

    def for_entity(self, entity_type: str, entity_id: str) -> "ActivityRecordQuerySet":
        """Narrow to the facts about one entity.

        ``entity_id`` is the business code (``PRJ-01``), never a primary key, which is what lets
        a task or blocker record be found without joining into another context. Together with
        :meth:`newest_first` this matches ``activity_entity_recent_idx`` exactly.

        Args:
            entity_type: ``project`` | ``task`` | ``blocker``, from
                :class:`ActivityRecord.EntityType`.
            entity_id: Business code of the entity.
        """
        return self.filter(entity_type=entity_type, entity_id=entity_id)

    def for_project(self, project_code: str) -> "ActivityRecordQuerySet":
        """Narrow to the facts about one project.

        Spelled separately from :meth:`for_entity` because the entity type is a constant the
        caller should not have to remember — a caller passing ``"projects"`` would silently get
        an empty timeline instead of an error.

        Args:
            project_code: ``Project.code``, e.g. ``PRJ-01``.
        """
        return self.for_entity(ActivityRecord.EntityType.PROJECT, project_code)

    def about(
        self, entity_type: str | None = None, entity_id: str | None = None
    ) -> "ActivityRecordQuerySet":
        """Narrow to an entity, to a kind of entity, or to neither.

        The optional form of :meth:`for_entity`, and the reason the portfolio-wide feed contains
        no branch: "everything about blockers", "everything about ``PRJ-22``" and "everything"
        are one call with different arguments. Passing both is exactly :meth:`for_entity` and
        matches ``activity_entity_recent_idx``; passing only ``entity_type`` scans that index's
        leading column without an equality prefix on the rest, so it is read with a page limit and
        never materialised whole.

        Args:
            entity_type: ``project`` | ``task`` | ``blocker``, from
                :class:`ActivityRecord.EntityType`. ``None`` leaves the kind open. An unknown
                value selects nothing rather than raising — the caller is a query string, not a
                developer.
            entity_id: Business code of the entity. ``None`` leaves it open.
        """
        selection = self
        if entity_type is not None:
            selection = selection.filter(entity_type=entity_type)
        if entity_id is not None:
            selection = selection.filter(entity_id=entity_id)
        return selection

    def with_verbs(self, verbs: Sequence[str]) -> "ActivityRecordQuerySet":
        """Narrow to the records stating any of ``verbs``.

        ORs its values, because an operator asking for state changes *and* blocker events wants
        both kinds of row rather than the empty intersection. An empty sequence means "no verb
        facet" and returns the selection untouched, so a caller never has to branch around it.

        Args:
            verbs: Values from :class:`ActivityRecord.Verb`. Unrecognised ones simply match
                nothing: the verb set is versioned by migration and a retired verb must not turn
                a working feed into an error.
        """
        if not verbs:
            return self
        return self.filter(verb__in=verbs)

    def by_actor(self, actor: str) -> "ActivityRecordQuerySet":
        """Narrow to what one actor caused, human or otherwise.

        ``actor`` is the alias the record was written with (``camila``, ``system``), not a foreign
        key: the trail outlives the account, so an actor whose user row is gone still reads back.
        With :meth:`newest_first` this matches ``activity_actor_recent_idx``.

        Args:
            actor: The alias stored on the record.
        """
        return self.filter(actor=actor)

    def from_origin(self, origin: str) -> "ActivityRecordQuerySet":
        """Narrow to the facts caused by one kind of author.

        This is the coarse form of :meth:`caused_by_people`, and the two are not
        interchangeable: that one encodes the staleness rule ("anything but the engine"), while
        this one answers an operator's explicit question, including "show me only ``POLICY``" —
        which is how the engine's own behaviour is audited.

        Args:
            origin: ``MANUAL`` | ``POLICY`` | ``SYSTEM``, from :class:`ActivityRecord.Origin`.
        """
        return self.filter(origin=origin)

    def occurred_between(
        self, since: datetime | None = None, until: datetime | None = None
    ) -> "ActivityRecordQuerySet":
        """Narrow to the facts that happened inside a time window.

        Both bounds are inclusive and both are optional, so one call expresses "since", "until",
        "between" and "no window at all" — which is what keeps the four-way branch out of every
        read service. The window is applied to ``occurred_at``, the time the fact happened, never
        to insertion order: a record written late still belongs to the moment it describes.

        Args:
            since: Lower bound, inclusive. ``None`` leaves the past open.
            until: Upper bound, inclusive. ``None`` leaves the present open.
        """
        selection = self
        if since is not None:
            selection = selection.filter(occurred_at__gte=since)
        if until is not None:
            selection = selection.filter(occurred_at__lte=until)
        return selection

    def for_correlation(self, correlation_id: UUID) -> "ActivityRecordQuerySet":
        """Narrow to every record written under one ``correlation_id``.

        This is what makes a reprioritization defensible: "project B was raised" and "project A
        was lowered to make room" are separate rows, and only this predicate shows they were one
        decision. Read it with :meth:`oldest_first`, since a decision is a sequence of
        consequences rather than a feed.

        Args:
            correlation_id: The id threaded through the use case that produced the records.
        """
        return self.filter(correlation_id=correlation_id)

    def caused_by_people(self) -> "ActivityRecordQuerySet":
        """Narrow to the facts somebody caused, excluding the ranking engine's own bookkeeping.

        This is what "days without recorded activity" has to mean. The engine recalculating a
        score is not somebody moving the work, and counting it makes the ``staleness`` signal
        read its own output: a project crosses the threshold, its score changes, the
        recomputation writes a ``POLICY`` record, and the next evaluation finds fresh activity
        and un-stales the project — an oscillation with no input from the portfolio at all.

        A ``MANUAL`` override stays in. A human forcing a rank *is* attention paid to the
        project, and it is the ``origin`` column — not the verb — that tells the two apart.
        """
        return self.exclude(origin=ActivityRecord.Origin.POLICY)

    def newest_first(self) -> "ActivityRecordQuerySet":
        """Order as a feed: most recent fact first, ties broken by insertion order."""
        return self.order_by("-occurred_at", "-id")

    def oldest_first(self) -> "ActivityRecordQuerySet":
        """Order as a narrative: the first consequence first."""
        return self.order_by("occurred_at", "id")

    def recent(self, limit: int = TIMELINE_PAGE_SIZE) -> "ActivityRecordQuerySet":
        """Take the first ``limit`` rows of the current ordering.

        Ends the chain for filtering: the queryset is still lazy but sliced, so no caller can
        add a predicate after it. Applied on top of :meth:`newest_first` the limit stops the
        index scan instead of sorting the table.

        Args:
            limit: Page size. Defaults to the rows the detail view renders.
        """
        return self[:limit]

    def as_entries(self) -> list[ActivityEntry]:
        """Materialise the selection as the frozen values the reads render.

        Ends the chain: the query executes here, so nothing downstream can resolve a field
        lazily after the transaction that produced the rows has closed.
        """
        return [record.to_entry() for record in self]


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
        """What kind of thing the record is about.

        ``MEMBER`` is the roster: a person's ``accounts.User.code`` is the ``entity_id``, exactly
        as a project's is its ``PRJ-NN``. Who joined, who was retired and whose capacity was
        raised are operational facts with the same standing as a state change — the ``OWNER_LOAD``
        risk flag is computed against the capacity somebody typed, so a flag nobody can explain is
        a flag nobody trusts.
        """

        PROJECT = "project", "Project"
        TASK = "task", "Task"
        BLOCKER = "blocker", "Blocker"
        MEMBER = "member", "Member"
        # The vocabulary itself. A taxonomy row is what every other context compares against, so
        # renaming or retiring one changes what half the product means; the trail is what makes
        # that editable from the product at all rather than only from the admin.
        ROLE = "role", "Role"
        # The lifecycle itself, addressed by ``Workflow.code``. Its states and its edges are
        # recorded under the graph rather than under themselves, because a state ``code`` is unique
        # only inside its workflow: ``bloqueada`` is not an identifier the trail could address, and
        # a reader asking "what happened to this lifecycle" wants the nodes, the arrows and the
        # renames on one timeline anyway.
        WORKFLOW = "workflow", "Workflow"

    class Verb(models.TextChoices):
        """The closed set of facts the trail can state (DATA_MODEL §5).

        There is deliberately no generic ``UPDATED``. Each verb names *which* fact moved, so the
        timeline reads as a sentence and a reader filtering on ``CAPACITY_CHANGED`` gets exactly
        the decisions that changed what "overloaded" means — which a catch-all verb with the field
        buried in ``metadata`` could only answer by scanning every row.
        """

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
        RENAMED = "RENAMED", "Renamed"
        ROLE_CHANGED = "ROLE_CHANGED", "Role changed"
        CAPACITY_CHANGED = "CAPACITY_CHANGED", "Capacity changed"
        DEACTIVATED = "DEACTIVATED", "Deactivated"
        REACTIVATED = "REACTIVATED", "Reactivated"
        # The fact that a password was replaced, and nothing about the password itself: no
        # ``from_value``, no ``to_value``, no hash. The trail answers "who reset whose credential
        # and when", which is the only question an audit can legitimately ask of this row.
        PASSWORD_RESET = "PASSWORD_RESET", "Password reset"
        # Authoring a lifecycle: the six facts an operator can state about the shape of a graph.
        # ``STATE_CHANGED`` is not one of them and must never be reused for one — it means "a record
        # moved", and a verb that meant both would make "how often did work get blocked" unanswerable.
        # A node's label, category, colour and order collapse into one ``STATE_EDITED`` on purpose:
        # they are one act of authoring performed in one form, and four verbs would report four
        # decisions where an operator made one. Which of them moved is in ``metadata.changed``.
        STATE_ADDED = "STATE_ADDED", "State added"
        STATE_EDITED = "STATE_EDITED", "State edited"
        STATE_RETIRED = "STATE_RETIRED", "State retired"
        TRANSITION_ADDED = "TRANSITION_ADDED", "Transition added"
        TRANSITION_EDITED = "TRANSITION_EDITED", "Transition edited"
        TRANSITION_RETIRED = "TRANSITION_RETIRED", "Transition retired"

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

    objects = ActivityRecordQuerySet.as_manager()

    class Meta:
        """Ordering and the indexes DATA_MODEL §10.2 names, each for a stated query."""

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
            # The portfolio-wide feed (`GET /api/v1/activity`), unfiltered or filtered only on a
            # low-selectivity column such as `origin`: no equality prefix, so none of the four
            # above can serve it and the planner would sort the whole table to return 50 rows.
            # Matches the model ordering exactly, which is what lets the first page stop after
            # 50 index entries.
            models.Index(fields=["-occurred_at", "-id"], name="activity_recent_idx"),
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
        ``objects.for_project(code).newest_first().recent().as_entries()`` for
        ``GET /api/v1/projects/{code}/timeline`` and
        ``objects.for_correlation(id).oldest_first().as_entries()`` for the "why did this move"
        panel, which both serialize outside the transaction that queried them. Handing the model
        out instead would let the router resolve fields lazily after that transaction closed,
        and would tie the response shape to the column list.

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
