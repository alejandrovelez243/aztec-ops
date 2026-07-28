"""Compatibility alias for the outbox write port, which lives in ``apps.events.services``.

`docs/standards/PATTERNS_BACKEND.md` §1 and `docs/standards/BACKEND.md` §4 both sketch the import
as ``from apps.events.outbox import enqueue_event``. The port itself is a use case and therefore
belongs under ``services`` per ARCHITECTURE §7, so this module exists only so the documented
import path keeps resolving. There is one implementation; do not add a second here.
"""

from apps.events.services import enqueue_event

__all__ = ["enqueue_event"]
