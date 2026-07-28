"""Django app configuration for the ``work`` context."""

from django.apps import AppConfig


class WorkConfig(AppConfig):
    """Tasks, dependencies, blockers and notes.

    Nothing outside this context holds a foreign key into it: ``work`` points at
    ``portfolio``'s published aggregate root, at ``catalog`` and at ``workflow``, and
    everything downstream learns about a task through events.
    """

    name = "apps.work"
    verbose_name = "Work"
