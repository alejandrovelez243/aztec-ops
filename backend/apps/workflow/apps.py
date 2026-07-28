"""Django app configuration for the workflow bounded context."""

from django.apps import AppConfig


class WorkflowConfig(AppConfig):
    """The configurable state machines: workflows, states, transitions and bindings.

    The guard registry needs no hook here: `domain/guards/__init__.py` imports every guard module,
    and the transition service imports that package, so a guard cannot be resolvable without also
    being registered.
    """

    name = "apps.workflow"
    verbose_name = "Workflow"
