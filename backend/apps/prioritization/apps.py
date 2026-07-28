"""App configuration for the prioritization context."""

from django.apps import AppConfig


class PrioritizationConfig(AppConfig):
    """Registers the prioritization models, admin and signal registry."""

    name = "apps.prioritization"
