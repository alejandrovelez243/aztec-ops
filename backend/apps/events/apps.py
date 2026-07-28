"""App configuration for the events context."""

from django.apps import AppConfig
from django.utils.module_loading import autodiscover_modules


class EventsConfig(AppConfig):
    """Wires the bus and discovers the handlers other contexts declare.

    Autodiscovery matters because a handler only exists once its ``@register_handler`` function has
    been imported. Leaving that to the caller means a reactor that runs or does not depending on
    which module happened to be imported first — the kind of bug that only shows up in the
    container. The module name is fixed: ``apps/<context>/handlers.py``.
    """

    name = "apps.events"
    verbose_name = "Events"

    def ready(self) -> None:
        """Import every ``apps/<context>/handlers`` module so its handlers self-register."""
        autodiscover_modules("handlers")
