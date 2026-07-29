"""The task detail reads a task's comments, so the task's comments get an index.

``work_note_project_recent`` serves the project timeline. Scoped to one task, that index is a scan
of every note the project ever collected, discarding the ones that name no task or another one.

Partial on ``task IS NOT NULL`` because most notes hang off the project alone and the task detail
excludes those by definition: indexing them would add write cost to every project note in order to
serve a query that can never return it.
"""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("portfolio", "0003_risk_flags_computed_on_read"),
        ("work", "0001_initial"),
    ]

    operations = [
        migrations.AddIndex(
            model_name="note",
            index=models.Index(
                condition=models.Q(("task__isnull", False)),
                fields=["task", "-created_at"],
                name="work_note_task_recent",
            ),
        ),
    ]
