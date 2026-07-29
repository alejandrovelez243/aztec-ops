"""The one place a typed domain error becomes an HTTP status.

Every non-2xx the application produces leaves through this module, in the single shape
`docs/API.md` §1.5 fixes:

```json
{"code": "transition_not_allowed", "message": "...", "details": {...}}
```

**No router catches a domain error.** A ``try/except DomainError`` inside a view is a bug, and it
is a bug for a specific reason: the mapping from "the blocker was already resolved" to 409 would
then exist in as many places as there are routes that can raise it, and the fourth one would
disagree. Adding an error means adding one row to :data:`_DESCRIPTORS` — never a branch in a view.

The client **switches on ``code``, never on ``message``** (`docs/API.md` §4.1). ``message`` is
English written for a developer reading a log; the Spanish UI maps ``code`` to its own text. That
is why the messages here are not translated and why changing one is not a breaking change.

Each bounded context owns the root of its own exception tree (``domain/`` may not import another
app), so this module registers one handler per root and routes them all through one function.
``500`` is deliberately not in the table: an exception that reaches the fallback is a bug, and
dressing it up as a typed rejection would hide it.
"""

import logging
from collections.abc import Callable, Mapping
from functools import partial
from typing import Final

from django.http import HttpRequest, HttpResponse, JsonResponse
from ninja import NinjaAPI
from ninja.errors import ValidationError as NinjaValidationError
from pydantic import JsonValue

from apps.accounts.domain import errors as accounts_errors
from apps.activity.domain import errors as activity_errors
from apps.catalog.domain import errors as catalog_errors
from apps.portfolio.domain import errors as portfolio_errors
from apps.prioritization.domain import errors as prioritization_errors
from apps.shared.ordering import UnknownOrdering
from apps.work.domain import errors as work_errors
from apps.workflow.domain import errors as workflow_errors

logger = logging.getLogger(__name__)

#: Stable machine strings. The frontend branches on these and on nothing else, so they are
#: constants rather than literals sprinkled through the descriptors below.
CODE_NOT_FOUND: Final = "not_found"
CODE_TRANSITION_NOT_ALLOWED: Final = "transition_not_allowed"
CODE_CONFLICTING_STATE: Final = "conflicting_state"
CODE_VALIDATION_ERROR: Final = "validation_error"
CODE_AUTHENTICATION_REQUIRED: Final = "authentication_required"
#: A wire ``code`` the frontend switches on, not a credential — hence the bandit suppression.
CODE_INVALID_TOKEN: Final = "invalid_token"  # noqa: S105
CODE_INVALID_CREDENTIALS: Final = "invalid_credentials"
CODE_PERMISSION_DENIED: Final = "permission_denied"

#: Applied to a ``DomainError`` subclass no descriptor names. 400 rather than 500 because the
#: error is typed and therefore deliberate — the context meant to reject the request — but the API
#: has nothing specific to say about it. A new error class showing up as a 400 with an empty
#: ``details`` is the signal to add a row below.
_FALLBACK_STATUS: Final = 400
_FALLBACK_CODE: Final = "domain_error"

#: The exception roots this module maps. One per bounded context, plus the two read-parameter
#: errors that belong to no context and ninja's own request-validation failure. Registering roots
#: rather than leaves is what lets a context add an error class without touching this file.
_HANDLED_ROOTS: Final[tuple[type[Exception], ...]] = (
    accounts_errors.AccountsError,
    activity_errors.ActivityError,
    catalog_errors.CatalogError,
    portfolio_errors.DomainError,
    prioritization_errors.DomainError,
    work_errors.DomainError,
    workflow_errors.DomainError,
    UnknownOrdering,
)


def _not_found(entity: str) -> Callable[[Exception], dict[str, JsonValue]]:
    """Build a ``{entity, id}`` details renderer for a ``*NotFound`` carrying one identifier."""

    def render(exc: Exception) -> dict[str, JsonValue]:
        return {"entity": entity, "id": _first_identifier(exc)}

    return render


def _conflict(entity: str, current: str) -> Callable[[Exception], dict[str, JsonValue]]:
    """Build a ``{entity, id, current}`` details renderer for a state conflict."""

    def render(exc: Exception) -> dict[str, JsonValue]:
        return {"entity": entity, "id": _first_identifier(exc), "current": current}

    return render


def _field(field_name: str) -> Callable[[Exception], dict[str, JsonValue]]:
    """Build a ``{fields: {name: [message]}}`` details renderer naming one offending field.

    The field name is what lets the UI point at one input instead of showing a banner, which is the
    difference between an operator fixing the request and an operator giving up on it.
    """

    def render(exc: Exception) -> dict[str, JsonValue]:
        return {"fields": {field_name: [str(exc)]}}

    return render


def _no_details(_exc: Exception) -> dict[str, JsonValue]:
    """Details for an error whose message already says everything. Never ``null``, per §1.5."""
    return {}


def _ops_lead_details(exc: Exception) -> dict[str, JsonValue]:
    """Name the action that was refused and the capability it needs.

    ``required`` is a capability name, not a taxonomy code: the rule is ``User.is_ops_lead`` and the
    UI branches on the ``is_ops_lead`` flag the token endpoints already return, so nothing here
    invites the client to compare against a ``catalog.Role`` label.
    """
    return {"required": "ops_lead", "action": getattr(exc, "action", "")}


def _taxonomy_not_found(exc: Exception) -> dict[str, JsonValue]:
    """Render ``{entity, id}`` for a taxonomy row addressed by a code that resolves to nothing."""
    return {
        "entity": str(getattr(exc, "taxonomy", "")),
        "id": str(getattr(exc, "code", "")),
    }


def _taxonomy_conflict(exc: Exception) -> dict[str, JsonValue]:
    """Render ``{entity, id, current}`` for a taxonomy code that is already taken.

    ``entity`` is read off the error rather than fixed, because one class serves every taxonomy
    and a hardcoded ``"role"`` would start lying the day a second one becomes writable.
    """
    return {
        "entity": str(getattr(exc, "taxonomy", "")),
        "id": str(getattr(exc, "code", "")),
        "current": "exists",
    }


def _password_details(exc: Exception) -> dict[str, JsonValue]:
    """Render every rule the proposed password broke under ``fields.password``.

    A list rather than one joined string, because the form shows them as a list and a client that
    had to split on sentence boundaries would be parsing prose. The rejected value is nowhere in
    here: the validators describe the rule, never the input.
    """
    problems = getattr(exc, "problems", ())
    return {"fields": {"password": [str(problem) for problem in problems] or [str(exc)]}}


def _transition_details(exc: Exception) -> dict[str, JsonValue]:
    """Render ``{from_state, to_state, allowed[]}`` so a stale client resyncs from the rejection.

    ``allowed`` is read off the error rather than re-queried here: the workflow service computed it
    while it still held the aggregate, and this handler runs after that transaction closed.
    """
    return {
        "from_state": getattr(exc, "from_state", ""),
        "to_state": getattr(exc, "to_state", ""),
        "allowed": list(getattr(exc, "allowed", ())),
    }


def _required_field_details(exc: Exception) -> dict[str, JsonValue]:
    """Render ``{fields: {<the aggregate field>: [message]}}`` for ``RequiredFieldMissing``."""
    return {"fields": {getattr(exc, "field_name", "unknown"): [str(exc)]}}


def _cycle_details(exc: Exception) -> dict[str, JsonValue]:
    """Render the dependency chain that would have closed, so the operator can break it."""
    return {"fields": {"depends_on": [str(exc)]}, "cycle": list(getattr(exc, "cycle", ()))}


def _ordering_details(exc: Exception) -> dict[str, JsonValue]:
    """Render the allowlist alongside the rejection: what may be sorted by, not only what may not."""
    return {"fields": {"order_by": [str(exc)]}, "allowed": list(getattr(exc, "allowed", ()))}


#: Exception class → (status, wire code, details renderer). The whole HTTP mapping of the
#: application, in one table. Ordered by status for reading; lookup walks the MRO, so a subclass
#: inherits its base's row and only needs an entry of its own when it says something more.
_DESCRIPTORS: Final[
    Mapping[type[Exception], tuple[int, str, Callable[[Exception], dict[str, JsonValue]]]]
] = {
    # --- 404: the identifier names nothing -------------------------------------------------
    portfolio_errors.ProjectNotFound: (404, CODE_NOT_FOUND, _not_found("project")),
    prioritization_errors.ProjectNotFound: (404, CODE_NOT_FOUND, _not_found("project")),
    work_errors.ProjectNotFound: (404, CODE_NOT_FOUND, _not_found("project")),
    work_errors.TaskNotFound: (404, CODE_NOT_FOUND, _not_found("task")),
    work_errors.BlockerNotFound: (404, CODE_NOT_FOUND, _not_found("blocker")),
    work_errors.PriorityNotFound: (404, CODE_NOT_FOUND, _not_found("priority")),
    work_errors.PersonNotFound: (404, CODE_NOT_FOUND, _not_found("person")),
    accounts_errors.MemberNotFound: (404, CODE_NOT_FOUND, _not_found("person")),
    # More specific than its ``UnknownCode`` base, which stays 422: the engine failing to resolve
    # a code it read is a broken contract, while a client editing a row somebody deleted has
    # simply addressed something that is not there.
    catalog_errors.TaxonomyRowNotFound: (404, CODE_NOT_FOUND, _taxonomy_not_found),
    # --- 401 / 403: who is asking, and whether they may -------------------------------------
    # Three distinct 401 codes rather than one, because the client's next move differs: no
    # credential means "sign in", a refused token means "refresh first", and bad credentials mean
    # "the form was answered wrongly". A single ``unauthorized`` would make the frontend guess.
    accounts_errors.AuthenticationRequired: (
        401,
        CODE_AUTHENTICATION_REQUIRED,
        _no_details,
    ),
    accounts_errors.TokenRejected: (401, CODE_INVALID_TOKEN, _no_details),
    accounts_errors.InvalidCredentials: (401, CODE_INVALID_CREDENTIALS, _no_details),
    accounts_errors.OpsLeadRequired: (403, CODE_PERMISSION_DENIED, _ops_lead_details),
    # 403 rather than 422: the request is well-formed, the caller holds the capability, and the
    # answer is still no because of *who* they are. Naming the subject lets the UI disable the
    # control on that one row instead of showing a banner after the fact.
    accounts_errors.CannotDeactivateSelf: (
        403,
        CODE_PERMISSION_DENIED,
        _not_found("person"),
    ),
    # --- 409: legal in the workflow, impossible against current facts ------------------------
    workflow_errors.TransitionNotAllowed: (
        409,
        CODE_TRANSITION_NOT_ALLOWED,
        _transition_details,
    ),
    workflow_errors.GuardRejected: (409, CODE_TRANSITION_NOT_ALLOWED, _no_details),
    portfolio_errors.DuplicateProjectCode: (
        409,
        CODE_CONFLICTING_STATE,
        _conflict("project", "exists"),
    ),
    work_errors.BlockerAlreadyResolved: (
        409,
        CODE_CONFLICTING_STATE,
        _conflict("blocker", "resolved"),
    ),
    work_errors.DependencyCycle: (409, CODE_CONFLICTING_STATE, _cycle_details),
    accounts_errors.DuplicateMemberCode: (
        409,
        CODE_CONFLICTING_STATE,
        _conflict("person", "exists"),
    ),
    # ``current: "exists"`` covers a retired row too, on purpose: the code is taken either way,
    # and the client's move — restore it, do not create a second one — is the same.
    catalog_errors.DuplicateTaxonomyCode: (
        409,
        CODE_CONFLICTING_STATE,
        _taxonomy_conflict,
    ),
    # --- 422: the request is well-formed and the domain refuses it ---------------------------
    workflow_errors.ReasonRequired: (422, CODE_VALIDATION_ERROR, _field("reason")),
    workflow_errors.RequiredFieldMissing: (
        422,
        CODE_VALIDATION_ERROR,
        _required_field_details,
    ),
    workflow_errors.WorkflowNotConfigured: (422, CODE_VALIDATION_ERROR, _no_details),
    workflow_errors.GuardNotRegistered: (422, CODE_VALIDATION_ERROR, _no_details),
    portfolio_errors.ClientNotFound: (422, CODE_VALIDATION_ERROR, _field("client")),
    portfolio_errors.OwnerNotFound: (422, CODE_VALIDATION_ERROR, _field("owner")),
    portfolio_errors.EngagementTypeNotFound: (
        422,
        CODE_VALIDATION_ERROR,
        _field("engagement_type"),
    ),
    portfolio_errors.ProjectTypeNotFound: (422, CODE_VALIDATION_ERROR, _field("project_type")),
    portfolio_errors.StageNotFound: (422, CODE_VALIDATION_ERROR, _field("stage")),
    portfolio_errors.CurrencyNotFound: (422, CODE_VALIDATION_ERROR, _field("currency")),
    portfolio_errors.InvalidDateWindow: (422, CODE_VALIDATION_ERROR, _field("target_date")),
    portfolio_errors.NegativeBusinessValue: (
        422,
        CODE_VALIDATION_ERROR,
        _field("business_value"),
    ),
    prioritization_errors.OverrideReasonRequired: (422, CODE_VALIDATION_ERROR, _field("reason")),
    prioritization_errors.OverrideMechanismAmbiguous: (
        422,
        CODE_VALIDATION_ERROR,
        _field("position"),
    ),
    work_errors.TaskOutsideProject: (422, CODE_VALIDATION_ERROR, _field("task_code")),
    work_errors.DependencyOutsideProject: (422, CODE_VALIDATION_ERROR, _field("depends_on")),
    work_errors.ResolutionReasonRequired: (422, CODE_VALIDATION_ERROR, _field("resolution")),
    accounts_errors.RoleNotFound: (422, CODE_VALIDATION_ERROR, _field("role")),
    accounts_errors.PasswordRejected: (422, CODE_VALIDATION_ERROR, _password_details),
    catalog_errors.UnknownCode: (422, CODE_VALIDATION_ERROR, _no_details),
    activity_errors.ActivityError: (422, CODE_VALIDATION_ERROR, _no_details),
    UnknownOrdering: (422, CODE_VALIDATION_ERROR, _ordering_details),
}


def register_exception_handlers(api: NinjaAPI) -> None:
    """Wire every mapped exception root, plus ninja's own, onto one API instance.

    Called once from :mod:`config.api`. Ninja resolves a handler by walking the raised exception's
    MRO, so registering the roots covers every subclass a context adds later; :data:`_DESCRIPTORS`
    is what decides whether that subclass gets its own status or its base's.

    Args:
        api: The API to attach the handlers to.
    """
    for root in _HANDLED_ROOTS:
        api.add_exception_handler(root, partial(_domain_error_response, api=api))
    api.add_exception_handler(NinjaValidationError, partial(_request_validation_response, api=api))


def domain_error_json(exc: Exception) -> JsonResponse:
    """Render a typed error in the §1.5 envelope for a view ninja does not serve.

    ``GET /api/stream`` is a plain Django view — its body is an open-ended byte stream, not a
    schema — so it cannot reach ninja's exception handlers. It still has to refuse an
    unauthenticated caller in the *same* shape as every other route, or the frontend would need a
    second parser for the one endpoint it keeps open permanently. Same table, same statuses, one
    contract.

    Args:
        exc: The typed error to render.

    Returns:
        The envelope, with the status :data:`_DESCRIPTORS` assigns the exception.
    """
    status, code, render_details = _descriptor_for(exc)
    return JsonResponse(
        {"code": code, "message": str(exc), "details": render_details(exc)}, status=status
    )


def _domain_error_response(request: HttpRequest, exc: Exception, *, api: NinjaAPI) -> HttpResponse:
    """Render one typed domain error in the §1.5 envelope."""
    status, code, render_details = _descriptor_for(exc)
    return _envelope(
        api,
        request,
        status=status,
        code=code,
        message=str(exc),
        details=render_details(exc),
    )


def _request_validation_response(
    request: HttpRequest, exc: NinjaValidationError, *, api: NinjaAPI
) -> HttpResponse:
    """Re-shape ninja's request-validation failure into the *same* envelope.

    The client has exactly one parser for every non-2xx (`docs/API.md` §1.5). Ninja's default body
    is ``{"detail": [...]}``, which would be a second shape the frontend has to recognise, so the
    per-field messages are folded into ``details.fields`` under the same ``validation_error`` code
    the domain uses for the same class of problem.
    """
    fields: dict[str, JsonValue] = {}
    for error in exc.errors:
        location = error.get("loc") or ("body",)
        # ``loc`` is ("body", "payload", "field"); the last segment is the one a form can point at.
        name = str(location[-1])
        fields.setdefault(name, [])
        messages = fields[name]
        if isinstance(messages, list):
            messages.append(str(error.get("msg", "invalid value")))
    return _envelope(
        api,
        request,
        status=422,
        code=CODE_VALIDATION_ERROR,
        message="The request did not validate.",
        details={"fields": fields},
    )


def _descriptor_for(
    exc: Exception,
) -> tuple[int, str, Callable[[Exception], dict[str, JsonValue]]]:
    """Find the most specific descriptor for this exception by walking its MRO.

    A context that adds an error class without adding a row here still gets a typed 400 rather than
    a 500, and the empty ``details`` is what makes the omission visible in the response.
    """
    for cls in type(exc).__mro__:
        descriptor = _DESCRIPTORS.get(cls)
        if descriptor is not None:
            return descriptor
    logger.warning(
        "no HTTP descriptor for domain error; add a row to config.errors._DESCRIPTORS",
        extra={"error_class": type(exc).__qualname__},
    )
    return (_FALLBACK_STATUS, _FALLBACK_CODE, _no_details)


def _envelope(
    api: NinjaAPI,
    request: HttpRequest,
    *,
    status: int,
    code: str,
    message: str,
    details: dict[str, JsonValue],
) -> HttpResponse:
    """Serialize the one error shape. ``details`` is an object, possibly empty, never ``null``."""
    return api.create_response(
        request,
        {"code": code, "message": message, "details": details},
        status=status,
    )


def _first_identifier(exc: Exception) -> str:
    """The business identifier a ``*NotFound`` carries, whatever its attribute is called.

    Each context names the attribute after what was missing — ``project_code``, ``task_code``,
    ``blocker_id`` — which is right for the raise site and inconvenient for exactly one reader.
    Rather than force seven error classes to share one attribute name, the reader adapts.
    """
    for attribute in ("project_code", "task_code", "blocker_id", "person_code", "priority_code"):
        value = getattr(exc, attribute, None)
        if value is not None:
            return str(value)
    return ""
