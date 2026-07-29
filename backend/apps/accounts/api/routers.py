"""Routes of the identity context: sign in, renew, sign out.

The first two are the only routes on the API besides the health probes that declare ``auth=None``,
and they have to: a caller with no token is exactly who they are for. The exception list is stated
once, in :mod:`config.api`, so adding a fourth unauthenticated route is a visible decision rather
than a quiet ``auth=None``.

Every one of the three touches the access cookie, and that is the only reason they take ninja's
injected ``response``: the cookie is what ``EventSource`` will send to ``GET /api/stream``, which
cannot carry an ``Authorization`` header (:mod:`config.auth`). The body carries the same token for
``fetch`` and for ``curl``.
"""

from django.http import HttpRequest, HttpResponse
from ninja import Router
from ninja.responses import Status

from apps.accounts.api.schemas import CredentialsIn, RefreshIn
from apps.accounts.domain.value_objects import AccessGrant, TokenPair
from apps.accounts.services import refresh_access, sign_in
from config.auth import clear_access_cookie, set_access_cookie

router = Router(tags=["auth"])


@router.post("/auth/token", response=TokenPair, auth=None, url_name="token_obtain")
def post_token(request: HttpRequest, payload: CredentialsIn, response: HttpResponse) -> TokenPair:
    """Exchange a username and password for an access/refresh pair.

    The access token is returned in the body *and* set as an ``HttpOnly`` cookie. Both, not either:
    the body is what a ``fetch`` client attaches as ``Authorization: Bearer`` and what a test or a
    ``curl`` uses, while the cookie is the only way an ``EventSource`` can authenticate at all.

    The response also says who the caller is and whether they are an ops lead, so the frontend
    renders the priority-override control from what the API told it rather than from decoding a
    token it has no business parsing.

    Errors: ``401 invalid_credentials`` (unknown username, wrong password, inactive account — one
    answer for all three, so this route cannot enumerate accounts), ``422 validation_error``.
    """
    del request
    pair = sign_in(username=payload.username, password=payload.password)
    set_access_cookie(response, token=pair.access)
    return pair


@router.post("/auth/token/refresh", response=AccessGrant, auth=None, url_name="token_refresh")
def post_token_refresh(
    request: HttpRequest, payload: RefreshIn, response: HttpResponse
) -> AccessGrant:
    """Renew an access token from a refresh token, and re-set the cookie with it.

    ``auth=None`` on purpose: the whole point of this route is to be reachable with an access token
    that has already expired. The refresh token in the body is the credential.

    The account is re-read here, not trusted from the token's claims, so deactivating somebody takes
    effect within one access-token lifetime rather than within the refresh window.

    Errors: ``401 invalid_token`` (malformed, expired, not a refresh token, or an account that can
    no longer sign in), ``422 validation_error``.
    """
    del request
    grant = refresh_access(refresh_token=payload.refresh)
    set_access_cookie(response, token=grant.access)
    return grant


@router.post("/auth/logout", response={204: None}, url_name="logout")
def post_logout(request: HttpRequest, response: HttpResponse) -> Status[None]:
    """Clear the access cookie.

    Authenticated like every other route, which is not ceremony: an unauthenticated endpoint that
    deletes a cookie is a cross-origin nuisance that logs people out. It is also idempotent —
    clearing a cookie that is not there is a 204 — so a client retrying after a dropped response
    gets the same answer.

    **The tokens themselves are not revoked**, and that is written down rather than hidden. There is
    no blacklist table (see :mod:`apps.accounts.services.sign_in`). What this removes is the copy
    the browser sends automatically; the client drops the pair it holds in memory, and the access
    token expires on its own within minutes. Server-side revocation would need a durable blacklist
    and a reason to run it, and this product has neither yet.
    """
    del request
    clear_access_cookie(response)
    return Status(204, None)
