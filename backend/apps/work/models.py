"""Persistence for the ``work`` context: tasks, dependencies, blockers and notes.

Fields, constraints, indexes and ``__str__`` only. Every query lives in
``repositories.py`` and every rule that spans two rows lives in ``services/`` — the
cross-row invariants this module cannot express (a blocker's project matching its task's,
the acyclicity of the dependency graph) are listed in ``DATA_MODEL`` §9.3 with the reason
each one is enforced in Python.
"""

from __future__ import annotations

from typing import Any, Final

from django.conf import settings
from django.db import connection, models
from django.db.models import F, Q

from apps.work.domain.value_objects import BlockerKind

#: ``choices`` rendered from the domain enum, so the closed set is declared once. It is a
#: structural vocabulary, not a taxonomy: adding a member is a migration, by design.
BLOCKER_KIND_CHOICES = [(kind.value, kind.label) for kind in BlockerKind]

#: PostgreSQL sequences that mint the business codes, created by raw SQL in migration
#: ``work.0001_initial`` with a matching ``reverse_sql``.
BLOCKER_CODE_SEQUENCE: Final = "work_blocker_code_seq"
NOTE_CODE_SEQUENCE: Final = "work_note_code_seq"

BLOCKER_CODE_PREFIX: Final = "BLK"
NOTE_CODE_PREFIX: Final = "NOTE"

#: Minimum width of the numeric part (``BLK-0142``). It is a floor, not a limit: the 10000th
#: blocker is ``BLK-10000`` and stays sortable as a number, which is why nothing truncates here.
CODE_DIGITS: Final = 4


def _next_business_code(sequence: str, prefix: str) -> str:
    """Draw the next value of a PostgreSQL sequence and render it as a business code.

    A sequence rather than ``max(pk) + 1`` or a Python counter, and the reason is concurrency:
    both alternatives read a value, decide, then write, so two transactions inserting at the same
    instant compute the same number and the second one either dies on the unique constraint or —
    worse, if the constraint were ever dropped — reuses a code that already names another row.
    ``nextval`` is atomic and non-transactional: it never hands the same number to two callers,
    even inside concurrent transactions. Its counterpart is that a rolled-back insert burns its
    number, which is correct here — codes are never reused and never renumbered, so a gap in the
    series is a fact about the operation, not a defect to be compacted away.

    Args:
        sequence: Name of the sequence created by the migration.
        prefix: Code prefix (``BLK``, ``NOTE``).

    Returns:
        The rendered code, zero-padded to :data:`CODE_DIGITS` and growing past it naturally.

    Raises:
        django.db.utils.ProgrammingError: The sequence does not exist, which means the migration
            that creates it has not been applied. Failing here is deliberate: the alternative is
            a row inserted with no code.
    """
    with connection.cursor() as cursor:
        cursor.execute("SELECT nextval(%s)", [sequence])
        row = cursor.fetchone()
    return f"{prefix}-{int(row[0]):0{CODE_DIGITS}d}"


class Task(models.Model):
    """A unit of work inside a project, with its own workflow state.

    ``workflow_state`` is written only by the transition service; nothing else assigns it,
    which is what keeps the legal moves a table rather than a set of ``if`` statements. A
    task has no life outside its project, hence the cascade, while the assignee is nulled
    on delete because the historical fact that the work existed must outlive the roster.

    ``assignee`` points at ``AUTH_USER_MODEL``: the person assigned the work is the same
    person who signs in to move it, so there is one row per person and no nullable hop to
    traverse before a permission check can name the requester.
    """

    code = models.CharField(max_length=16)
    project = models.ForeignKey("portfolio.Project", on_delete=models.CASCADE, related_name="tasks")
    assignee = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="tasks",
    )
    priority = models.ForeignKey("catalog.Priority", on_delete=models.PROTECT, related_name="tasks")
    workflow_state = models.ForeignKey(
        "workflow.WorkflowState", on_delete=models.PROTECT, related_name="tasks"
    )
    due_date = models.DateField(null=True, blank=True)
    title = models.CharField(max_length=200)
    detail = models.TextField(default="", blank=True)
    last_progress = models.CharField(max_length=255, default="", blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "task"
        verbose_name_plural = "tasks"
        ordering = ["code"]
        constraints = [
            models.UniqueConstraint(fields=["code"], name="work_task_code_unique"),
        ]
        indexes = [
            models.Index(fields=["project", "workflow_state"], name="work_task_project_state"),
            models.Index(fields=["assignee", "workflow_state"], name="work_task_assignee_state"),
            models.Index(fields=["due_date"], name="work_task_due_date"),
        ]

    def __str__(self) -> str:
        return f"{self.code} {self.title}"


class TaskDependency(models.Model):
    """A "this cannot start until that is done" edge, resolved to a task or still free text.

    ``depends_on`` and ``raw_label`` both exist because 61 of the 82 source tasks name
    their prerequisite by title rather than by code, and many of those titles match
    nothing. An unresolvable dependency is preserved as prose instead of being dropped, and
    ``is_resolved`` is the stored form of ``depends_on IS NOT NULL`` so the admin's
    "unresolved dependencies" view is one filter rather than a null scan.
    """

    task = models.ForeignKey(Task, on_delete=models.CASCADE, related_name="dependencies")
    depends_on = models.ForeignKey(
        Task, on_delete=models.SET_NULL, null=True, blank=True, related_name="dependents"
    )
    raw_label = models.CharField(max_length=255, default="", blank=True)
    is_resolved = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "task dependency"
        verbose_name_plural = "task dependencies"
        ordering = ["task", "pk"]
        constraints = [
            # Unresolved rows repeat freely: the same prose legitimately appears twice.
            models.UniqueConstraint(
                fields=["task", "depends_on"],
                condition=Q(depends_on__isnull=False),
                name="work_taskdep_edge_unique",
            ),
            models.CheckConstraint(
                condition=Q(depends_on__isnull=False) | ~Q(raw_label=""),
                name="work_taskdep_has_target_or_label",
            ),
            # The only cycle a CHECK can catch; the general case is domain/dependencies.py.
            models.CheckConstraint(
                condition=Q(depends_on__isnull=True) | ~Q(depends_on=F("task")),
                name="work_taskdep_not_self",
            ),
        ]

    def __str__(self) -> str:
        target = self.depends_on.code if self.depends_on is not None else self.raw_label
        return f"{self.task.code} depends on {target}"


class Blocker(models.Model):
    """An impediment as a row, not as a substring of a notes field.

    Openness is ``resolved_at IS NULL`` and nothing else, which is what lets the blockage
    signal, the risk evaluator and the open-blockers panel agree on one definition.
    ``project`` is always populated — even for a task-level blocker, where the service
    copies it from the task — so any consumer can resolve a blocker to a portfolio row
    without joining through ``Task``.

    ``code`` is the business identifier the event envelope carries as ``entity.id``
    (EVENTS.md §1): a consumer in another context addresses a blocker by ``BLK-0142`` and
    never needs a foreign key into ``work``. It is assigned once, on insert, from
    ``work_blocker_code_seq``.

    This row is **not** append-only, unlike ``ActivityRecord``: resolving a blocker is an
    update of ``resolved_at`` and ``resolution_reason``, so ``save()`` must keep accepting
    updates. Only ``code`` is immutable — it is minted on the insert and never recomputed,
    because a code that can be renumbered is a code no audit trail can rely on.
    """

    code = models.CharField(max_length=16, blank=True)
    project = models.ForeignKey(
        "portfolio.Project", on_delete=models.CASCADE, related_name="blockers"
    )
    task = models.ForeignKey(
        Task, on_delete=models.CASCADE, null=True, blank=True, related_name="blockers"
    )
    description = models.TextField()
    kind = models.CharField(max_length=24, choices=BLOCKER_KIND_CHOICES)
    raised_at = models.DateTimeField(auto_now_add=True)
    resolved_at = models.DateTimeField(null=True, blank=True)
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="blockers",
    )
    resolution_reason = models.CharField(max_length=255, default="", blank=True)

    class Meta:
        verbose_name = "blocker"
        verbose_name_plural = "blockers"
        ordering = ["-raised_at"]
        constraints = [
            models.UniqueConstraint(fields=["code"], name="work_blocker_code_unique"),
            models.CheckConstraint(
                condition=Q(resolved_at__isnull=True) | Q(resolved_at__gte=F("raised_at")),
                name="work_blocker_resolved_after_raised",
            ),
            # Closing a blocker without saying how is how blockers come back.
            models.CheckConstraint(
                condition=Q(resolved_at__isnull=True) | ~Q(resolution_reason=""),
                name="work_blocker_resolution_reason",
            ),
        ]
        indexes = [
            # Partial: a resolved blocker is read by neither the panel nor the signal.
            models.Index(
                fields=["project"],
                condition=Q(resolved_at__isnull=True),
                name="work_blocker_open",
            ),
            models.Index(fields=["kind"], name="work_blocker_kind"),
            models.Index(fields=["raised_at"], name="work_blocker_raised_at"),
        ]

    def __str__(self) -> str:
        state = "resolved" if self.resolved_at else "open"
        return f"{self.code} {self.kind} blocker ({state})"

    def save(self, *args: Any, **kwargs: Any) -> None:
        """Mint ``code`` on the insert, then save normally.

        The code is drawn from ``work_blocker_code_seq`` and only when the row is being
        inserted, which is what makes it immutable: an update — resolving the blocker — never
        reaches the generator, so the identifier a consumer already saw on ``blocker.raised``
        still names the same row on ``blocker.resolved``. This is *not* the append-only
        ``ActivityRecord`` guard: updates are legitimate here and are never refused.

        An explicit ``code`` is honoured so a fixture can pin one; ``loaddata`` bypasses this
        method entirely (``save_base(raw=True)``), so seeds must carry their own codes.

        Raises:
            django.db.utils.IntegrityError: The explicit code collides with an existing row.
                Generated codes cannot collide — ``nextval`` never repeats.
        """
        if self._state.adding and not self.code:
            self.code = _next_business_code(BLOCKER_CODE_SEQUENCE, BLOCKER_CODE_PREFIX)
        super().save(*args, **kwargs)


class Note(models.Model):
    """A chronological comment on a project, or on one task of it.

    ``author`` is a denormalized ``accounts.User.code`` rather than a foreign key, and it
    stays that way now that the assignee *is* a user: a note survives its author's row being
    deleted, and it also records authors who are not people at all — the engine and the
    consumers write as ``system``. ``project`` is always set, copied from the task for
    task-scoped notes, so the project timeline never needs a join to find them.

    ``code`` is the business identifier the ``note.added`` envelope carries as ``entity.id``
    (``NOTE-0391``), so a consumer addresses a note without a foreign key into ``work``. It is
    assigned once, on insert, from ``work_note_code_seq``.

    A note is not append-only at the database level — the body can be corrected — but its
    ``code`` is immutable: it is minted on the insert and never recomputed, because the code is
    what an already-published event points at.
    """

    code = models.CharField(max_length=16, blank=True)
    project = models.ForeignKey("portfolio.Project", on_delete=models.CASCADE, related_name="notes")
    task = models.ForeignKey(
        Task, on_delete=models.CASCADE, null=True, blank=True, related_name="notes"
    )
    body = models.TextField()
    author = models.CharField(max_length=32)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "note"
        verbose_name_plural = "notes"
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(fields=["code"], name="work_note_code_unique"),
        ]
        indexes = [
            models.Index(fields=["project", "-created_at"], name="work_note_project_recent"),
        ]

    def __str__(self) -> str:
        return f"{self.code} by {self.author} at {self.created_at:%Y-%m-%d}"

    def save(self, *args: Any, **kwargs: Any) -> None:
        """Mint ``code`` on the insert, then save normally.

        Same contract as :meth:`Blocker.save`: the sequence is read only while the row is being
        inserted, so an edit to the body cannot renumber a note that a published ``note.added``
        event already names. Updates are allowed — this is not the append-only
        ``ActivityRecord`` guard.

        An explicit ``code`` is honoured so a fixture can pin one; ``loaddata`` bypasses this
        method entirely (``save_base(raw=True)``), so seeds must carry their own codes.

        Raises:
            django.db.utils.IntegrityError: The explicit code collides with an existing row.
                Generated codes cannot collide — ``nextval`` never repeats.
        """
        if self._state.adding and not self.code:
            self.code = _next_business_code(NOTE_CODE_SEQUENCE, NOTE_CODE_PREFIX)
        super().save(*args, **kwargs)
