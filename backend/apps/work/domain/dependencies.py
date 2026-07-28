"""Acyclicity of the task dependency graph.

``DATA_MODEL`` §9.3 puts this rule in the application layer on purpose: a ``CHECK`` cannot
express reachability, and a trigger running ``WITH RECURSIVE`` on every insert would be
paid on all 82 seed rows to catch a case the service already refuses. The check is a pure
function over an adjacency mapping so it can be exercised with ``SimpleTestCase``, which
forbids database access — the caller loads the graph, this module only decides.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

#: ``graph[task_code]`` is the codes that ``task_code`` depends on, i.e. its prerequisites.
#: Edges point from dependent to prerequisite, the same direction ``TaskDependency`` stores
#: (``task`` -> ``depends_on``). Unresolved rows contribute no edge and cannot form a cycle.
DependencyGraph = Mapping[str, Sequence[str]]


def find_dependency_cycle(
    *, graph: DependencyGraph, task_code: str, depends_on_code: str
) -> tuple[str, ...] | None:
    """Return the loop that adding ``task_code -> depends_on_code`` would close, if any.

    Adding the edge creates a cycle exactly when ``depends_on_code`` can already reach
    ``task_code`` by following prerequisites, so the search starts at the prerequisite and
    looks for the dependent. The graph is read as given: codes absent from it have no
    prerequisites, which is what makes a partially loaded project graph safe to pass.

    Args:
        graph: Existing edges, prerequisite lists keyed by dependent task code. Must not
            already contain the proposed edge, or the result is the trivial loop.
        task_code: The task that would gain the prerequisite.
        depends_on_code: The task that would become the prerequisite.

    Returns:
        The cycle as a path starting and ending at ``task_code`` — the chain an operator
        has to break — or ``None`` when the edge keeps the graph acyclic.
    """
    if task_code == depends_on_code:
        return (task_code, task_code)

    stack: list[tuple[str, tuple[str, ...]]] = [(depends_on_code, (task_code, depends_on_code))]
    visited: set[str] = set()

    while stack:
        current, path = stack.pop()
        if current == task_code:
            return path
        if current in visited:
            continue
        visited.add(current)
        stack.extend(
            (prerequisite, (*path, prerequisite)) for prerequisite in graph.get(current, ())
        )

    return None
