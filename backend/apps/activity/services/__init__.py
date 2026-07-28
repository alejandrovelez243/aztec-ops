"""Use cases of the activity context.

The context has exactly one: appending a fact. ``write_activity`` is re-exported here so a
caller in another context writes ``from apps.activity.services import write_activity`` without
having to know which module holds it.
"""

from apps.activity.services.record import write_activity

__all__ = ["write_activity"]
