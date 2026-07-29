"""Typed failures raised by the ``work`` context.

Every error carries the identifiers a caller needs to render a message without
re-querying, because the API maps these to status codes in one central handler and that
handler has no access to the aggregate that raised them.

The base class is declared here rather than imported from a shared package: ``domain/``
may not import another app (``ARCHITECTURE`` §7), so each context owns the root of its own
exception tree and the HTTP handler registers one mapping per context.
"""

from __future__ import annotations


class DomainError(Exception):
    """Root of every failure the ``work`` context raises deliberately.

    Anything escaping a service that is *not* a ``DomainError`` is a bug rather than a
    rejected request, and the central handler maps it to 500 on purpose.
    """


class TaskNotFound(DomainError):
    """No task carries the given business code.

    Mapped to 404. Raised before any mutation, so the transaction has written nothing.
    """

    def __init__(self, task_code: str) -> None:
        super().__init__(f"No task with code {task_code!r}.")
        self.task_code = task_code


class ProjectNotFound(DomainError):
    """The project a task, blocker or note was to be attached to does not exist.

    Mapped to 404. ``work`` names projects by business code and never by primary key, so
    a stale code from the UI surfaces here instead of as an integrity error at commit.
    """

    def __init__(self, project_code: str) -> None:
        super().__init__(f"No project with code {project_code!r}.")
        self.project_code = project_code


class PriorityNotFound(DomainError):
    """The requested ``catalog.Priority`` code is not in the taxonomy.

    Mapped to 404. Priorities are operator-editable rows, so an inactive or renamed code
    is a normal failure and not a programming error.
    """

    def __init__(self, priority_code: str) -> None:
        super().__init__(f"No priority with code {priority_code!r}.")
        self.priority_code = priority_code


class PersonNotFound(DomainError):
    """No person carries the code given as an assignee or as a blocker owner.

    Named after what is missing — a person — rather than after the table that used to hold
    one: assignees and blocker owners are ``accounts.User`` rows now, and an error named
    ``TeamMemberNotFound`` would outlive the model it was named for.

    Mapped to 404. Clearing an assignee is expressed as an explicit ``None``, so reaching
    this error always means a code was supplied and did not resolve.
    """

    def __init__(self, person_code: str) -> None:
        super().__init__(f"No person with code {person_code!r}.")
        self.person_code = person_code


class TaskOutsideProject(DomainError):
    """A task was referenced from a project that does not own it.

    Mapped to 422. This is the application-level half of the rule in ``DATA_MODEL`` §9.3:
    ``Blocker.project_id`` and ``Note.project_id`` are written by the service *from the
    task*, so a caller naming a mismatched pair is rejected rather than silently
    reparented.
    """

    def __init__(self, task_code: str, project_code: str) -> None:
        super().__init__(f"Task {task_code!r} does not belong to project {project_code!r}.")
        self.task_code = task_code
        self.project_code = project_code


class DependencyOutsideProject(DomainError):
    """A dependency pointed at a task in a different project.

    Mapped to 422. Cross-project prerequisites would make the acyclicity check span the
    whole portfolio instead of one project's adjacency, and the source data never does it.
    """

    def __init__(self, task_code: str, depends_on_code: str) -> None:
        super().__init__(
            f"Task {task_code!r} cannot depend on {depends_on_code!r}: different projects."
        )
        self.task_code = task_code
        self.depends_on_code = depends_on_code


class DependencyCycle(DomainError):
    """The requested prerequisite closes a loop in the dependency graph.

    Mapped to 409. ``cycle`` is the path that would have been created, first element to
    last, so the API can show the operator which chain to break instead of only refusing.
    """

    def __init__(self, cycle: tuple[str, ...]) -> None:
        super().__init__("Dependency cycle: " + " -> ".join(cycle))
        self.cycle = cycle


class BlockerNotFound(DomainError):
    """No blocker carries the given primary key.

    Mapped to 404. Blockers are addressed by primary key rather than by a business code
    because they have none in the source data.
    """

    def __init__(self, blocker_id: int) -> None:
        super().__init__(f"No blocker with id {blocker_id}.")
        self.blocker_id = blocker_id


class BlockerAlreadyResolved(DomainError):
    """The blocker was closed before this call reached it.

    Mapped to 409. Raised under ``select_for_update``, so it is a genuine second
    resolution and not a lost update: re-resolving would overwrite the first resolver's
    reason and re-emit ``blocker.resolved``.
    """

    def __init__(self, blocker_id: int) -> None:
        super().__init__(f"Blocker {blocker_id} is already resolved.")
        self.blocker_id = blocker_id


class ResolutionReasonRequired(DomainError):
    """A blocker was closed without saying how.

    Mapped to 422, and the application half of the ``resolved_at IS NULL OR
    resolution_reason <> ''`` check. Enforced here as well as in the database so the
    operator gets a message instead of an integrity error.
    """

    def __init__(self, blocker_id: int) -> None:
        super().__init__(f"Blocker {blocker_id} cannot be resolved without a reason.")
        self.blocker_id = blocker_id
