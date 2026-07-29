"""Workflow use cases. One module per use case, one public function each.

Three families, and the boundary between them is the point of the whole context. **Obedience** is
``transition``: whether the record in front of you may make the move it is asking for, decided
against the rows every time it is asked. **Authoring** is the eight after it: what shape the graph
has at all. Authoring writes what obedience reads, and never the other way round — no authoring
module imports ``transition``, and ``transition`` imports none of them. **Reassignment** is
``reassignment``, the third: not what the graph looks like and not whether a move is legal, but
which graph a given record answers to at all. Like ``transition`` it validates and reports, leaving
the write to the context that owns the record.

Re-exported here so a router or a test imports from ``apps.workflow.services`` without having to
know which module holds which. ``_authoring`` stays private: it protects no invariant of its own and
is only how the eight address a graph the same way.
"""

from apps.workflow.services.add_state import add_state
from apps.workflow.services.add_transition import add_transition
from apps.workflow.services.create_workflow import create_workflow
from apps.workflow.services.read_workflows import read_workflows
from apps.workflow.services.reassignment import validate_reassignment
from apps.workflow.services.retire_state import retire_state
from apps.workflow.services.retire_transition import retire_transition
from apps.workflow.services.transition import validate_transition
from apps.workflow.services.update_state import update_state
from apps.workflow.services.update_transition import update_transition
from apps.workflow.services.update_workflow import update_workflow

__all__ = [
    "add_state",
    "add_transition",
    "create_workflow",
    "read_workflows",
    "retire_state",
    "retire_transition",
    "update_state",
    "update_transition",
    "update_workflow",
    "validate_reassignment",
    "validate_transition",
]
