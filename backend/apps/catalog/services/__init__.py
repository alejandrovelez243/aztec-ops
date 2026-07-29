"""Use cases of the catalog context: one read, and the two writes that touch roles.

Five of the six taxonomies are still admin-only, and the exception is narrow on purpose. A role is
the one vocabulary an operator needs *while doing something else* — registering somebody who does a
job nobody has typed yet — and sending them to ``/admin/`` mid-form is how a person ends up filed
under the wrong role permanently. An engagement type or a workflow stage is a decision about how the
business works; it is made deliberately, in the admin.

The objection this module used to record — that a taxonomy editor over HTTP is a second,
*unaudited* editor of the vocabulary every other context compares against — is answered rather than
dropped: :mod:`apps.catalog.services.write_role` writes an ``ActivityRecord`` for every change it
makes, so a renamed or retired role is as reconstructible as a project's state change.
"""

from apps.catalog.services.read_catalog import read_catalog
from apps.catalog.services.read_roles import DEFAULT_ROLE_STATUS, RoleStatus, read_roles
from apps.catalog.services.write_role import create_role, update_role

__all__ = [
    "DEFAULT_ROLE_STATUS",
    "RoleStatus",
    "create_role",
    "read_catalog",
    "read_roles",
    "update_role",
]
