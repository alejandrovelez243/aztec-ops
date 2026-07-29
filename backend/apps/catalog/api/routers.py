"""Routes of the catalog context: the taxonomies, and the one of them that is editable here.

Reading the vocabulary is everybody's business. Writing it is an ops lead's, and only for roles —
the other five taxonomies are decisions about how the business works and are still made in the
admin. The narrowness is the point: a product screen that could invent an engagement type would be
a second, competing definition of what the portfolio is.
"""

from uuid import uuid4

from django.http import HttpRequest
from django.utils import timezone
from ninja import Router
from ninja.responses import Status

from apps.catalog.api.schemas import RoleCreateIn, RoleUpdateIn
from apps.catalog.domain.commands import CreateRoleCommand, UpdateRoleCommand
from apps.catalog.domain.value_objects import CatalogView
from apps.catalog.services import (
    DEFAULT_ROLE_STATUS,
    RoleStatus,
    create_role,
    read_catalog,
    read_roles,
    update_role,
)
from apps.shared.refs import TaxonomyRef
from config.auth import actor_code_of, roster_admin

router = Router(tags=["catalog"])


@router.get("/catalog", response=CatalogView, url_name="catalog_read")
def get_catalog(request: HttpRequest) -> CatalogView:
    """Return every active taxonomy value the frontend renders a picker from.

    Engagement types, project types, stages, priorities, roles and currencies, each list in the
    order an operator arranged it.

    Unauthenticated like every other read, and deliberately one document: a form needs all six
    lists before it can draw itself, and six requests to draw one form is six chances to render
    half a page.

    Currencies carry ``minor_units`` beside ``code`` and ``label``. It is the only field a client
    cannot derive — 28000 CLP is $28.000 and 28000 USD is $280.00 — so a frontend that formatted
    with a hardcoded 2 would be wrong by two orders of magnitude for every zero-decimal currency.

    Workflow states are **not** here: a state is scoped to its workflow and the legal moves out of
    one are ``transitions`` on the project detail (`docs/API.md` §2.2). A global state list would
    invite the client to guess legality, which is exactly what that field exists to prevent.
    """
    del request
    return read_catalog()


@router.get(
    "/catalog/roles",
    response=list[TaxonomyRef],
    auth=roster_admin,
    url_name="role_list",
)
def get_roles(request: HttpRequest, status: RoleStatus = DEFAULT_ROLE_STATUS) -> list[TaxonomyRef]:
    """Every role, including the retired ones, in the operator's own order.

    Separate from ``GET /catalog`` rather than a flag on it, and the split is the point: the
    catalog is the **pickers**, and a retired value must never appear in one. This route is the
    *editor's* view, which has to show the retired rows or restoring one would be impossible —
    a retire button with no way back is a delete with better manners.

    Ops lead, like the writes it accompanies: knowing which roles were retired is not something a
    reader needs in order to read the roster.
    """
    del request
    return read_roles(status=status)


@router.post(
    "/catalog/roles",
    response={201: TaxonomyRef},
    auth=roster_admin,
    url_name="role_create",
)
def post_role(request: HttpRequest, payload: RoleCreateIn) -> Status[TaxonomyRef]:
    """Add a role to the vocabulary, active and last in the picker.

    The one taxonomy writable from the product, because it is the one an operator needs while
    doing something else: registering somebody who does a job nobody has typed yet. Every change
    writes an ``ActivityRecord``, so the vocabulary has the same audit trail as the work.

    A code already in use is ``409 conflicting_state`` — **including a retired role's**. The row
    still exists and is merely out of the pickers, so the fix is to restore it rather than create
    a second one that would resolve ambiguously ever after.

    Errors: ``409 conflicting_state``, ``422 validation_error``, ``403 permission_denied``.
    """
    return Status(
        201,
        create_role(
            CreateRoleCommand(code=payload.code, label=payload.label),
            actor=actor_code_of(request),
            correlation_id=uuid4(),
            now=timezone.now(),
        ),
    )


@router.patch(
    "/catalog/roles/{role_code}",
    response=TaxonomyRef,
    auth=roster_admin,
    url_name="role_update",
)
def patch_role(request: HttpRequest, role_code: str, payload: RoleUpdateIn) -> TaxonomyRef:
    """Rename a role, retire it, or restore it. Absent means untouched.

    Retiring is ``is_active = false`` and never a delete: the role leaves the pickers and keeps
    resolving on the people already classified under it, so nobody is silently unclassified.

    ``code`` is not writable. It is what those people carry, and what a payload or a filter
    compares against; the label is what changes when the wording does.

    Errors: ``404 not_found``, ``422 validation_error``, ``403 permission_denied``.
    """
    fields = payload.model_dump(exclude_unset=True)
    return update_role(
        UpdateRoleCommand(code=role_code, **fields),
        actor=actor_code_of(request),
        correlation_id=uuid4(),
        now=timezone.now(),
    )
