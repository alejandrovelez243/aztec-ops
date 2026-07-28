"""Resolution of the business codes a portfolio command arrives with.

Private to ``services/``. These are primary-key-equivalent lookups with no joins and no domain
predicate, which PATTERNS §2 explicitly says do not earn a repository; what they *do* earn is one
place that turns "no such row" into the right typed error, so three use cases cannot disagree about
whether an unknown engagement type is a 404 or a 422.

``resolve_owner`` is the exception that proves the rule: the person it looks up belongs to another
context, so it delegates to ``apps.accounts.repositories`` instead of touching that context's
manager, and only the error mapping is done here.
"""

from apps.accounts.models import User
from apps.accounts.repositories import user_by_code
from apps.catalog.models import EngagementType, ProjectType, Stage
from apps.portfolio.domain.errors import (
    ClientNotFound,
    EngagementTypeNotFound,
    OwnerNotFound,
    ProjectTypeNotFound,
    StageNotFound,
)
from apps.portfolio.models import Client


def resolve_client(client_code: str | None) -> Client:
    """Resolve a required client reference.

    Args:
        client_code: ``Client.code``. None is rejected: a project always has a counterparty, so
            "clear the client" is not an operation.

    Returns:
        The client row.

    Raises:
        ClientNotFound: The code is None or matches no row.
    """
    if client_code is None:
        raise ClientNotFound("")
    client = Client.objects.filter(code=client_code).first()
    if client is None:
        raise ClientNotFound(client_code)
    return client


def resolve_engagement_type(engagement_type_code: str | None) -> EngagementType:
    """Resolve a required engagement type.

    It is required because it drives both the score modifier and the workflow binding: a project
    without one could not be ranked or transitioned.

    Args:
        engagement_type_code: ``EngagementType.code``.

    Returns:
        The engagement type row.

    Raises:
        EngagementTypeNotFound: The code is None or matches no row.
    """
    if engagement_type_code is None:
        raise EngagementTypeNotFound("")
    engagement_type = EngagementType.objects.filter(code=engagement_type_code).first()
    if engagement_type is None:
        raise EngagementTypeNotFound(engagement_type_code)
    return engagement_type


def resolve_project_type(project_type_code: str | None) -> ProjectType | None:
    """Resolve an optional project type.

    Args:
        project_type_code: ``ProjectType.code``, or None to leave the project unclassified.

    Returns:
        The project type row, or None.

    Raises:
        ProjectTypeNotFound: A code was given and matches no row.
    """
    if project_type_code is None:
        return None
    project_type = ProjectType.objects.filter(code=project_type_code).first()
    if project_type is None:
        raise ProjectTypeNotFound(project_type_code)
    return project_type


def resolve_stage(stage_code: str | None) -> Stage | None:
    """Resolve an optional delivery stage.

    Args:
        stage_code: ``Stage.code``, or None.

    Returns:
        The stage row, or None.

    Raises:
        StageNotFound: A code was given and matches no row.
    """
    if stage_code is None:
        return None
    stage = Stage.objects.filter(code=stage_code).first()
    if stage is None:
        raise StageNotFound(stage_code)
    return stage


def resolve_owner(owner_code: str | None) -> User | None:
    """Resolve an optional owner.

    None is a legitimate value, not a missing one: an unowned project is itself an operational
    signal and the queue is expected to surface it.

    Inactive people still resolve, because ``apps.accounts.repositories.user_by_code`` deliberately
    does not filter on ``is_active``: retiring someone must not make every project they own
    unsaveable.

    Args:
        owner_code: ``accounts.User.code``, or None to leave the project unowned.

    Returns:
        The person, or None.

    Raises:
        OwnerNotFound: A code was given and matches nobody.
    """
    if owner_code is None:
        return None
    owner = user_by_code(owner_code)
    if owner is None:
        raise OwnerNotFound(owner_code)
    return owner
