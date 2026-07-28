"""Celery application.

Celery is the transport. Producers still write only to the transactional outbox (``CLAUDE.md``
rule 4); ``events.drain_outbox`` claims the committed rows and dispatches one ``events.handle_event``
per subscribed handler, and Beat both sweeps the outbox and emits the clock ticks. There is one job
system, not two: the hand-rolled Redis Streams consumer groups it replaces did the same work with a
second retry policy, a second dead-letter path and three more processes.
"""

import os

from celery import Celery

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

app = Celery("aztec_ops")

# Config comes from Django settings under a CELERY_ prefix, so there is one place to look.
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()
