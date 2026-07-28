"""Typed errors raised by the portfolio services.

Every error carries the identifiers a caller needs to render a message, so the central ninja
exception handler maps a class to a status code without parsing prose. Services raise these and
never catch them: a ``try/except`` around a domain error in a service would put the HTTP mapping in
two places (BACKEND §5).

Suggested mapping: ``*NotFound`` → 404, ``DuplicateProjectCode`` → 409, everything else → 422.
"""


class DomainError(Exception):
    """Base class for every business-rule violation raised by this context.

    Defined per context because ``domain/`` may not import another app. If a shared base is later
    introduced, this class becomes its subclass and no raise site changes.
    """


class ProjectNotFound(DomainError):
    """No project exists for the given business code."""

    def __init__(self, project_code: str) -> None:
        super().__init__(f"No project with code {project_code}.")
        self.project_code = project_code


class DuplicateProjectCode(DomainError):
    """A project already uses the requested code.

    Raised before the insert so the caller gets a 409 instead of an ``IntegrityError`` surfacing as
    a 500 from the unique constraint.
    """

    def __init__(self, project_code: str) -> None:
        super().__init__(f"A project with code {project_code} already exists.")
        self.project_code = project_code


class ClientNotFound(DomainError):
    """No client exists for the given code."""

    def __init__(self, client_code: str) -> None:
        super().__init__(f"No client with code {client_code}.")
        self.client_code = client_code


class OwnerNotFound(DomainError):
    """No person exists for the code offered as a project owner.

    Named after the reference that failed rather than after a table, because the portfolio resolves
    a person for exactly one purpose. Owning nobody is legal — ``owner_code=None`` leaves a project
    unowned, which is itself an operational signal — so this is only ever a code that resolves to
    nothing, never a missing value.
    """

    def __init__(self, owner_code: str) -> None:
        super().__init__(f"No person with code {owner_code}.")
        self.owner_code = owner_code


class TaxonomyEntryNotFound(DomainError):
    """A referenced catalog row does not exist, or was retired and then deleted.

    One subclass per taxonomy rather than a taxonomy-name argument: five taxonomies all key on
    ``code``, so "no row for ``consultoria``" is ambiguous, and a caller that wants to catch only a
    bad stage should not have to inspect a string to do it.
    """

    taxonomy = "catalog entry"

    def __init__(self, code: str) -> None:
        super().__init__(f"No {self.taxonomy} with code {code!r}.")
        self.code = code


class EngagementTypeNotFound(TaxonomyEntryNotFound):
    """No engagement type carries the given code."""

    taxonomy = "engagement type"


class ProjectTypeNotFound(TaxonomyEntryNotFound):
    """No project type carries the given code."""

    taxonomy = "project type"


class StageNotFound(TaxonomyEntryNotFound):
    """No delivery stage carries the given code."""

    taxonomy = "stage"


class InvalidDateWindow(DomainError):
    """``start_date`` is later than ``target_date``.

    Either date may legitimately be null — 9 source projects have no start and 5 have no target —
    but an inverted pair is data corruption, so it is rejected at the service as well as by the
    database check constraint.
    """

    def __init__(self, start_date: str, target_date: str) -> None:
        super().__init__(f"start_date {start_date} is after target_date {target_date}.")
        self.start_date = start_date
        self.target_date = target_date


class NegativeBusinessValue(DomainError):
    """A contract value below zero was supplied."""

    def __init__(self, business_value: str) -> None:
        super().__init__(f"business_value {business_value} is negative.")
        self.business_value = business_value
