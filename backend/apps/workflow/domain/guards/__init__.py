"""Guards: named, pure decisions a `WorkflowTransition` can require.

Importing this package is what populates the registry — each guard module registers itself on
import, so `resolve_guard` cannot answer for a module nobody loaded. Adding a guard is one file
plus one `@register_guard("<code>")` line plus one line here; the transition service is never
edited.
"""

from apps.workflow.domain.guards.registry import (
    Guard,
    register_guard,
    registered_guard_codes,
    resolve_guard,
)
from apps.workflow.domain.guards.require_open_blocker import require_open_blocker

__all__ = [
    "Guard",
    "register_guard",
    "registered_guard_codes",
    "require_open_blocker",
    "resolve_guard",
]
