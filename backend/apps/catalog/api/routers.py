"""Route of the catalog context: the taxonomies the frontend renders its pickers from."""

from django.http import HttpRequest
from ninja import Router

from apps.catalog.domain.value_objects import CatalogView
from apps.catalog.services import read_catalog

router = Router(tags=["catalog"])


@router.get("/catalog", response=CatalogView, auth=None, url_name="catalog_read")
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
