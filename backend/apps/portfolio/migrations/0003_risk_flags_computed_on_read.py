"""The read model keeps the facts and loses the conclusions (ADR 0011).

``risk_flags`` and ``health`` were derived columns: a rebuild wrote what was true at the moment an
event was applied, and the row then went on claiming it. They are computed from the remaining
columns on every read instead, so the GIN index that made ``?risk_flag=BLOCKED`` a containment
lookup goes with them.

``in_progress_task_count`` arrives in their place. It is the one fact ``HasNoNextStep`` needs that
no other column carried, and it comes free from the aggregate that already produces the other four
task counts — the alternative being an ``exists()`` per project on every queue read.

The next rebuild of each row fills it; a row not yet rebuilt reads zero, which understates
"something is in progress" and can only raise ``NO_NEXT_STEP``, never hide it.
"""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("portfolio", "0002_project_currency_foreign_key")]

    operations = [
        migrations.RemoveIndex(
            model_name="projectsnapshot",
            name="portfolio_snap_riskflags_gin",
        ),
        migrations.AddField(
            model_name="projectsnapshot",
            name="in_progress_task_count",
            field=models.SmallIntegerField(default=0),
        ),
        migrations.RemoveField(
            model_name="projectsnapshot",
            name="health",
        ),
        migrations.RemoveField(
            model_name="projectsnapshot",
            name="risk_flags",
        ),
    ]
