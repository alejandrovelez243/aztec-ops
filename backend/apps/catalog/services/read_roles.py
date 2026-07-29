"""Use case: the role vocabulary as its *editor* sees it, retired rows included.

Deliberately not a flag on :func:`~apps.catalog.services.read_catalog.read_catalog`. That one
serves the **pickers**, and a retired value appearing in a picker is exactly what retiring is meant
to prevent — one endpoint that could return either would be one flag away from putting a retired
role back in front of every form in the product.

This one answers the opposite question: what is there to edit. A screen that offers "retire" and
cannot show what has been retired offers a delete with better manners.
"""

from typing import Literal

from apps.catalog.models import Role
from apps.shared.refs import TaxonomyRef

#: Which half of the vocabulary to read. Three values rather than a boolean, matching the roster's
#: own ``status``, so the two editors of "things that can be retired" answer the same three
#: questions in the same words.
RoleStatus = Literal["active", "inactive", "all"]

#: The editor's default: everything, because hiding rows from the screen whose job is to show what
#: exists is how somebody concludes a role was deleted.
DEFAULT_ROLE_STATUS: RoleStatus = "all"


def read_roles(*, status: RoleStatus = DEFAULT_ROLE_STATUS) -> list[TaxonomyRef]:
    """Return the roles matching ``status``, in the operator's own order.

    Args:
        status: ``active`` is what the pickers show, ``inactive`` is what has been retired, and
            ``all`` — the default — is what the editor needs.

    Returns:
        Each role as the reference every surface renders. Ordering is ``Meta.ordering``
        (``order``, then ``code``), so the editor's list and the pickers agree about sequence.
    """
    roles = Role.objects.all()
    if status == "active":
        roles = roles.active()
    elif status == "inactive":
        roles = roles.filter(is_active=False)
    return [role.to_ref() for role in roles]
