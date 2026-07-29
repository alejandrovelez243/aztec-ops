"""Use cases of the activity context: appending a fact, and reading the trail back.

Both are re-exported here so a caller in another context imports from
``apps.activity.services`` without having to know which module holds which. Appending is the only
*write*: there is no update and no delete, which is what makes the trail evidence rather than a
story somebody preferred afterwards.
"""

from apps.activity.services.read_timeline import TimelineFilters, read_project_timeline
from apps.activity.services.record import write_activity

__all__ = ["TimelineFilters", "read_project_timeline", "write_activity"]
