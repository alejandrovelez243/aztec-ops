"""Index the portfolio-wide activity feed.

``GET /api/v1/activity`` sorts on ``-occurred_at, -id`` with no equality prefix, so none of the
four indexes from ``0001_initial`` can serve it: each of those leads with a column the global feed
does not filter on. Without this one the planner sorts the whole trail to return fifty rows, and
the cost grows with every fact the portfolio ever recorded.
"""

from django.db import migrations, models


class Migration(migrations.Migration):
    """Add ``activity_recent_idx``, matching the model ordering exactly."""

    dependencies = [
        ("activity", "0001_initial"),
    ]

    operations = [
        migrations.AddIndex(
            model_name="activityrecord",
            index=models.Index(fields=["-occurred_at", "-id"], name="activity_recent_idx"),
        ),
    ]
