"""Initial ``work`` schema, plus the two sequences the business codes are drawn from.

``Blocker.save()`` and ``Note.save()`` mint ``BLK-0142`` / ``NOTE-0391`` with
``SELECT nextval(...)``, so the sequences are part of this schema and not an optional extra:
without them the first insert after ``migrate`` fails. They are created here rather than in a
follow-up migration because ``makemigrations`` cannot author them — no model state describes a
sequence — and a hand-written operation that lives apart from the tables it serves is the one
that gets lost the next time the migration set is regenerated.

The numbers come from PostgreSQL sequences rather than ``max(pk) + 1`` or a Python counter,
because those read-then-write and hand the same number to two concurrent transactions.
``nextval`` is atomic and non-transactional: a rolled-back insert burns its number, and the gap
is kept, because codes are never reused and never renumbered.
"""

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models

CREATE_BLOCKER_SEQUENCE = (
    "CREATE SEQUENCE IF NOT EXISTS work_blocker_code_seq AS bigint START WITH 1 INCREMENT BY 1;"
)
DROP_BLOCKER_SEQUENCE = "DROP SEQUENCE IF EXISTS work_blocker_code_seq;"

CREATE_NOTE_SEQUENCE = (
    "CREATE SEQUENCE IF NOT EXISTS work_note_code_seq AS bigint START WITH 1 INCREMENT BY 1;"
)
DROP_NOTE_SEQUENCE = "DROP SEQUENCE IF EXISTS work_note_code_seq;"


class Migration(migrations.Migration):
    initial = True

    dependencies = [
        ("catalog", "0001_initial"),
        ("portfolio", "0001_initial"),
        ("workflow", "0001_initial"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        # Before the tables: reversing this migration must drop the sequences too, or a
        # re-applied migration would keep counting from the old high-water mark.
        migrations.RunSQL(sql=CREATE_BLOCKER_SEQUENCE, reverse_sql=DROP_BLOCKER_SEQUENCE),
        migrations.RunSQL(sql=CREATE_NOTE_SEQUENCE, reverse_sql=DROP_NOTE_SEQUENCE),
        migrations.CreateModel(
            name="Task",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                ("code", models.CharField(max_length=16)),
                ("due_date", models.DateField(blank=True, null=True)),
                ("title", models.CharField(max_length=200)),
                ("detail", models.TextField(blank=True, default="")),
                ("last_progress", models.CharField(blank=True, default="", max_length=255)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "assignee",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="tasks",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "priority",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="tasks",
                        to="catalog.priority",
                    ),
                ),
                (
                    "project",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="tasks",
                        to="portfolio.project",
                    ),
                ),
                (
                    "workflow_state",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="tasks",
                        to="workflow.workflowstate",
                    ),
                ),
            ],
            options={
                "verbose_name": "task",
                "verbose_name_plural": "tasks",
                "ordering": ["code"],
            },
        ),
        migrations.CreateModel(
            name="Note",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                ("code", models.CharField(blank=True, max_length=16)),
                ("body", models.TextField()),
                ("author", models.CharField(max_length=32)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "project",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="notes",
                        to="portfolio.project",
                    ),
                ),
                (
                    "task",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="notes",
                        to="work.task",
                    ),
                ),
            ],
            options={
                "verbose_name": "note",
                "verbose_name_plural": "notes",
                "ordering": ["-created_at"],
            },
        ),
        migrations.CreateModel(
            name="Blocker",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                ("code", models.CharField(blank=True, max_length=16)),
                ("description", models.TextField()),
                (
                    "kind",
                    models.CharField(
                        choices=[
                            ("EXTERNAL_DEPENDENCY", "External dependency"),
                            ("ACCESS", "Access"),
                            ("DECISION", "Decision"),
                            ("TECHNICAL", "Technical"),
                        ],
                        max_length=24,
                    ),
                ),
                ("raised_at", models.DateTimeField(auto_now_add=True)),
                ("resolved_at", models.DateTimeField(blank=True, null=True)),
                ("resolution_reason", models.CharField(blank=True, default="", max_length=255)),
                (
                    "owner",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="blockers",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "project",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="blockers",
                        to="portfolio.project",
                    ),
                ),
                (
                    "task",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="blockers",
                        to="work.task",
                    ),
                ),
            ],
            options={
                "verbose_name": "blocker",
                "verbose_name_plural": "blockers",
                "ordering": ["-raised_at"],
            },
        ),
        migrations.CreateModel(
            name="TaskDependency",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                ("raw_label", models.CharField(blank=True, default="", max_length=255)),
                ("is_resolved", models.BooleanField(default=False)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "depends_on",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="dependents",
                        to="work.task",
                    ),
                ),
                (
                    "task",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="dependencies",
                        to="work.task",
                    ),
                ),
            ],
            options={
                "verbose_name": "task dependency",
                "verbose_name_plural": "task dependencies",
                "ordering": ["task", "pk"],
            },
        ),
        migrations.AddIndex(
            model_name="task",
            index=models.Index(
                fields=["project", "workflow_state"], name="work_task_project_state"
            ),
        ),
        migrations.AddIndex(
            model_name="task",
            index=models.Index(
                fields=["assignee", "workflow_state"], name="work_task_assignee_state"
            ),
        ),
        migrations.AddIndex(
            model_name="task",
            index=models.Index(fields=["due_date"], name="work_task_due_date"),
        ),
        migrations.AddConstraint(
            model_name="task",
            constraint=models.UniqueConstraint(fields=("code",), name="work_task_code_unique"),
        ),
        migrations.AddIndex(
            model_name="note",
            index=models.Index(fields=["project", "-created_at"], name="work_note_project_recent"),
        ),
        migrations.AddConstraint(
            model_name="note",
            constraint=models.UniqueConstraint(fields=("code",), name="work_note_code_unique"),
        ),
        migrations.AddIndex(
            model_name="blocker",
            index=models.Index(
                condition=models.Q(("resolved_at__isnull", True)),
                fields=["project"],
                name="work_blocker_open",
            ),
        ),
        migrations.AddIndex(
            model_name="blocker",
            index=models.Index(fields=["kind"], name="work_blocker_kind"),
        ),
        migrations.AddIndex(
            model_name="blocker",
            index=models.Index(fields=["raised_at"], name="work_blocker_raised_at"),
        ),
        migrations.AddConstraint(
            model_name="blocker",
            constraint=models.UniqueConstraint(fields=("code",), name="work_blocker_code_unique"),
        ),
        migrations.AddConstraint(
            model_name="blocker",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ("resolved_at__isnull", True),
                    ("resolved_at__gte", models.F("raised_at")),
                    _connector="OR",
                ),
                name="work_blocker_resolved_after_raised",
            ),
        ),
        migrations.AddConstraint(
            model_name="blocker",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ("resolved_at__isnull", True),
                    models.Q(("resolution_reason", ""), _negated=True),
                    _connector="OR",
                ),
                name="work_blocker_resolution_reason",
            ),
        ),
        migrations.AddConstraint(
            model_name="taskdependency",
            constraint=models.UniqueConstraint(
                condition=models.Q(("depends_on__isnull", False)),
                fields=("task", "depends_on"),
                name="work_taskdep_edge_unique",
            ),
        ),
        migrations.AddConstraint(
            model_name="taskdependency",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ("depends_on__isnull", False),
                    models.Q(("raw_label", ""), _negated=True),
                    _connector="OR",
                ),
                name="work_taskdep_has_target_or_label",
            ),
        ),
        migrations.AddConstraint(
            model_name="taskdependency",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ("depends_on__isnull", True),
                    models.Q(("depends_on", models.F("task")), _negated=True),
                    _connector="OR",
                ),
                name="work_taskdep_not_self",
            ),
        ),
    ]
