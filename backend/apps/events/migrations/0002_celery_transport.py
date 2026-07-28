"""Move the bus from Redis Streams to Celery.

``stream_entry_id`` and its constraint describe a transport that no longer exists.
``dead_lettered_at`` replaces the dead letter stream, and ``ProcessedEvent.consumer_group`` becomes
``handler``: the idempotency key is now ``(event_id, handler)``.
"""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("events", "0001_initial")]

    operations = [
        migrations.RemoveConstraint(
            model_name="outboxevent",
            name="events_outboxevent_published_has_entry_id",
        ),
        migrations.RemoveField(model_name="outboxevent", name="stream_entry_id"),
        migrations.AddField(
            model_name="outboxevent",
            name="dead_lettered_at",
            field=models.DateTimeField(blank=True, default=None, null=True),
        ),
        migrations.AddIndex(
            model_name="outboxevent",
            index=models.Index(
                condition=models.Q(("dead_lettered_at__isnull", False)),
                fields=["-dead_lettered_at"],
                name="events_outbox_deadletter_idx",
            ),
        ),
        migrations.RemoveConstraint(
            model_name="processedevent",
            name="events_processedevent_unique_per_group",
        ),
        migrations.RenameField(
            model_name="processedevent",
            old_name="consumer_group",
            new_name="handler",
        ),
        migrations.AddConstraint(
            model_name="processedevent",
            constraint=models.UniqueConstraint(
                fields=("event_id", "handler"),
                name="events_processedevent_unique_per_handler",
            ),
        ),
    ]
