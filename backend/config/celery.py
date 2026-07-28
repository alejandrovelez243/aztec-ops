"""Celery application.

Celery does one job here: scheduling. It is not the event bus — Redis Streams is, and every
producer still writes to the transactional outbox first (``CLAUDE.md`` rule 4). What Beat replaces
is the hand-rolled ticker loop and, more importantly, the state that loop had to keep to notice a
local date rollover. A crontab entry expresses "at midnight" without remembering anything.
"""

import os

from celery import Celery

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

app = Celery("aztec_ops")

# Config comes from Django settings under a CELERY_ prefix, so there is one place to look.
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()
