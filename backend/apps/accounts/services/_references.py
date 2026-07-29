"""Resolution of the one business code a roster command arrives with.

Private to ``services/``. A single-column lookup with no join and no domain predicate does not earn
a repository (PATTERNS §2); what it earns is one place that turns "no such row" into the right
typed error, so the create and the update use case cannot disagree about whether an unknown role is
a 404 or a 422.
"""

from apps.accounts.domain.errors import RoleNotFound
from apps.catalog.models import Role


def resolve_role(role_code: str | None) -> Role | None:
    """Resolve an optional role.

    ``None`` is a legitimate value and not a missing one: an unclassified person is a real state —
    the seeded ``admin`` account is one — and the roster renders it as "Sin rol" rather than
    refusing to show the row.

    Retired roles still resolve. This reads the unfiltered manager on purpose, exactly as the
    portfolio's ``resolve_owner`` does: deactivating a role must take it out of the picker, not
    make every person already classified under it unsaveable.

    Args:
        role_code: ``catalog.Role.code``, or ``None`` to leave the person unclassified.

    Returns:
        The role row, or ``None``.

    Raises:
        RoleNotFound: A code was given and matches no row.
    """
    if role_code is None:
        return None
    role = Role.objects.filter(code=role_code).first()
    if role is None:
        raise RoleNotFound(role_code)
    return role
