"""App configuration for the activity bounded context."""

from django.apps import AppConfig


class ActivityConfig(AppConfig):
    """Registers the append-only audit trail every other context writes to."""

    name = "apps.activity"
