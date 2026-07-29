"""The single ``NinjaAPI`` instance: every route, and the one place errors become statuses.

Mounted at ``/api/v1/`` by :mod:`config.urls`, which is what puts the OpenAPI document at
``/api/v1/openapi.json`` — the source `docs/API.md` §1.1 names for
``frontend/src/lib/api/types.ts``. API types are generated from that document and never
hand-written (CLAUDE.md rule 11), so this module's job is to make the document true.

The routers are assembled here rather than each app registering itself, because the URL space is a
property of the API and not of any one context: two contexts both serving paths under
``/projects/{code}/`` have to be composed by something that can see both. Every router declares
full paths and is mounted at the root, so a path in this codebase is greppable exactly as it
appears in a request log.

Breaking a response shape means ``/api/v2``, not an edit here. Adding an optional field to an
``*Out`` schema is not breaking.
"""

from ninja import NinjaAPI

from apps.accounts.api import router as accounts_router
from apps.activity.api import router as activity_router
from apps.catalog.api import router as catalog_router
from apps.portfolio.api import router as portfolio_router
from apps.prioritization.api import router as prioritization_router
from apps.work.api import router as work_router
from apps.workflow.api import router as workflow_router
from config.auth import token_auth
from config.errors import register_exception_handlers
from config.health import router as health_router

#: Bumped only for a breaking change, which is also when ``/api/v2`` appears beside this one.
API_VERSION = "1.0.0"

api = NinjaAPI(
    title="Aztec Ops API",
    version=API_VERSION,
    description=(
        "Operational portfolio command center. Authenticated with a JWT access token, sent as "
        "`Authorization: Bearer` or — for `EventSource`, which cannot set headers — as an HttpOnly "
        "cookie on safe methods. Any member may act on any project; the manual priority override "
        "and the portfolio-wide recompute additionally require an ops lead. Errors share one "
        "envelope: `{code, message, details}`; branch on `code`, never on `message`."
    ),
    urls_namespace="api-v1",
    # Authentication is the DEFAULT of the instance, not a per-router opt-in, so a route added
    # tomorrow is protected by the fact that its author did nothing. Declaring it per route is how
    # an endpoint ends up public because somebody forgot a line — the failure mode is silent, and
    # it fails open.
    #
    # THE COMPLETE LIST OF ROUTES THAT OPT OUT WITH ``auth=None``. Adding a sixth is a decision
    # somebody makes on purpose, and it is reviewed here:
    #   1. GET  /health/live      — a kubelet probe runs before anything can present a token.
    #   2. GET  /health/ready     — the load balancer's drain signal, same reason.
    #   3. GET  /health/pipeline  — operational counters, no business data.
    #   4. POST /auth/token       — obtaining a token cannot require a token.
    #   5. POST /auth/token/refresh — reachable precisely when the access token has expired.
    # Everything else, read or write, requires a valid access token.
    auth=token_auth,
    docs_url="/docs",
)

register_exception_handlers(api)

# Routers are mounted at the root and declare full paths. Two contexts serve paths under
# ``/projects/{code}/`` — the portfolio owns the project, ``work`` owns its tasks and blockers —
# and mounting by prefix would force the URL to decide which context owns a use case.
api.add_router("", accounts_router)
api.add_router("", catalog_router)
api.add_router("", workflow_router)
api.add_router("", portfolio_router)
api.add_router("", work_router)
api.add_router("", activity_router)
api.add_router("", prioritization_router)
# Not a bounded context: the health routes describe the deployment, not the business, which is why
# they are composed here rather than owned by an app. The pipeline numbers still come from a
# service in ``apps.events`` — a router that queried the outbox itself would be the one place in
# the codebase where ``api/`` reads a model.
api.add_router("", health_router)
