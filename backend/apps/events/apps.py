"""App configuration for the events context."""

from django.apps import AppConfig
from django.utils.module_loading import autodiscover_modules


class EventsConfig(AppConfig):
    """Wires the bus and discovers the consumers other contexts declare.

    Autodiscovery matters because a consumer only exists once its ``@register_consumer`` class has
    been imported. Leaving that to the caller means ``run_consumer <group>`` fails with "not
    registered" depending on which module happened to be imported first — the kind of bug that
    only shows up in the container.
    """

    name = "apps.events"
    verbose_name = "Events"

    def ready(self) -> None:
        """Import every ``apps/<context>/consumers`` package so its consumers self-register."""
        autodiscover_modules("consumers")
