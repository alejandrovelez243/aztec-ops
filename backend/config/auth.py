"""Real authentication: who this request is, and the two things not everybody may do.

This module replaces ``config/actor.py``. An ``X-Actor`` header let anyone declare themselves
anyone, which did not merely leave permissions unimplemented — it left them *unrepresentable*,
because there was no verified identity to attach one to, and it made ``ActivityRecord.actor`` a
claim rather than a fact. That field is the one an audit trail is worth nothing without.

## Two ways in, and why there have to be two

``EventSource`` cannot set a request header. There is no way for a browser to send
``Authorization: Bearer`` to ``GET /api/stream``, and the stream is the whole live surface of the
product. So the access token is *also* set as a cookie, which ``EventSource`` sends when the client
opens it with ``withCredentials: true``. That is what forces ``CORS_ALLOW_CREDENTIALS = True`` and,
with it, an explicit origin allowlist — a credentialed request may not be answered with ``*``.

Never in the query string. A token in a URL lands in the access log of every proxy between the
browser and this process, in the ``Referer`` of anything the page links to, and in browser history.
A header or a cookie is read by the process and by nothing else on the path.

## The cookie is accepted on safe methods only

``NinjaAPI`` marks its views CSRF-exempt, which is right for a header-authenticated API and fatal
for a cookie-authenticated one: a form on any origin could POST to a transition route and the
browser would attach the cookie for it. Restricting the cookie to ``GET``/``HEAD``/``OPTIONS``
removes that class of attack structurally instead of relying on ``SameSite`` alone, and it costs
nothing, because the cookie exists for exactly one caller — ``EventSource``, which can only issue a
``GET``. Everything that writes presents the header, which no cross-origin page can forge.
``SameSite`` is still set, as the second lock rather than the only one.

## What did *not* change

No service reads a request object, and no service signature moved: routers still pass an actor
*code* into a use case. ``request.user`` is now where that code comes from, which is the whole
substitution — confined to this module, exactly as ``config/actor.py`` promised it would be.
"""

from typing import Final, Literal

from django.conf import settings
from django.http import HttpRequest, HttpResponse
from ninja.security import HttpBearer
from ninja_jwt.authentication import JWTBaseAuthentication
from ninja_jwt.exceptions import AuthenticationFailed
from ninja_jwt.settings import api_settings

from apps.accounts.domain.errors import AuthenticationRequired, OpsLeadRequired, TokenRejected
from apps.accounts.models import User

#: Name of the access-token cookie. One constant, read by the auth classes, by the two routes that
#: set it and by the one that clears it, so the four cannot disagree about its spelling.
ACCESS_COOKIE_NAME: Final = "aztec_access"

#: Scoped to the API surface: ``/api/v1/...`` and ``/api/stream``. ``/admin/`` has its own session
#: cookie and has no business receiving this one, and a cookie scoped to ``/`` would be attached to
#: every static asset request for nothing.
ACCESS_COOKIE_PATH: Final = "/api/"

#: HTTP methods the cookie is honoured for — the ones that cannot change state, which is what makes
#: cookie authentication safe on a CSRF-exempt API. See the module docstring.
COOKIE_SAFE_METHODS: Final = frozenset({"GET", "HEAD", "OPTIONS"})

#: The scheme half of ``Authorization: Bearer <token>``, lower-cased for comparison.
BEARER_SCHEME: Final = "bearer"

#: ``Lax`` keeps the cookie off cross-site form posts entirely while still sending it with the
#: same-site ``GET`` an ``EventSource`` opens. A module constant rather than a setting because it
#: does not vary by environment — unlike ``AUTH_COOKIE_SECURE``, which has to, since a browser
#: discards a ``Secure`` cookie delivered over the plain HTTP the compose stack serves. It is also
#: the *second* lock: the cookie is refused outright on unsafe methods above.
COOKIE_SAMESITE: Final[Literal["Lax"]] = "Lax"

#: What :class:`OpsLeadAuth` reports as refused. One phrase for both routes because both are the
#: same capability — overruling the engine — and an operator reading a 403 needs to know which
#: capability they lack, not which URL they happened to hit.
OPS_LEAD_ACTION: Final = "Overriding the ranking or rebuilding the whole portfolio"


class TokenAuth(JWTBaseAuthentication, HttpBearer):
    """The default authentication of the whole API: a signed access token, header or cookie.

    Declared as a ninja security scheme rather than as middleware so the OpenAPI document says the
    API is authenticated, and the generated client knows it needs a token instead of discovering it
    as a 401.

    Every failure is an exception, never ``None``. Ninja reads ``None`` as "this scheme declined"
    and falls through to its own bare 401, whose body is a second error shape the frontend would
    have to parse; raising instead routes the rejection through :mod:`config.errors` and the one
    documented envelope.
    """

    def __call__(self, request: HttpRequest) -> User:
        """Resolve the caller, or refuse the request.

        Args:
            request: The incoming request.

        Returns:
            The authenticated person, which ninja also stores on ``request.auth``.

        Raises:
            AuthenticationRequired: No bearer header, and no cookie the method may be trusted with.
            TokenRejected: A token was presented and did not survive validation.
        """
        token = _presented_token(request)
        if token is None:
            raise AuthenticationRequired
        return self.authenticate(request, token)

    def authenticate(self, request: HttpRequest, token: str) -> User:
        """Validate the token, load the account, and apply this scheme's authorization rule.

        Args:
            request: The incoming request; ``request.user`` is populated as a side effect, which is
                what makes ``request.user`` the actor everywhere downstream.
            token: The raw JWT.

        Returns:
            The authenticated person.

        Raises:
            TokenRejected: Expired, malformed, wrong token type, or issued for an account that no
                longer exists or is no longer active.
        """
        try:
            user = self.jwt_authenticate(request, token)
        except AuthenticationFailed as error:
            raise TokenRejected from error
        if not isinstance(user, User):  # pragma: no cover - AUTH_USER_MODEL is accounts.User
            raise TokenRejected
        self.authorize(user)
        return user

    def authorize(self, user: User) -> None:
        """Decide whether this authenticated person may reach the route. Being signed in is enough.

        Aztec Ops is a Jira, not a filing cabinet: any member may act on any project or task —
        create, update, transition, raise and resolve blockers, add notes. "My tickets" is a filter
        (``Task.objects.assigned_to``), never a restriction, exactly as it is in Jira. Ownership
        scoping would stop a colleague unblocking a project while its owner is on holiday, which is
        the opposite of what an operational board is for.

        Args:
            user: The authenticated person. Unused here; :class:`OpsLeadAuth` is the override.
        """


class OpsLeadAuth(TokenAuth):
    """Authentication plus the one authorization rule this product has.

    Exactly two routes use it, and both override the engine rather than participate in it: forcing
    a project's rank against its computed score, and rebuilding the ranking of the entire
    portfolio. Everything else is collaborative by design.

    A subclass rather than a decorator or a permission list because ninja resolves ``auth`` per
    route: ``auth=ops_lead`` on the operation *is* the declaration, it appears in the OpenAPI
    document, and there is no second place where a route could be added to a protected set and
    forgotten.
    """

    def authorize(self, user: User) -> None:
        """Refuse anyone who is not an ops lead.

        Args:
            user: The authenticated person.

        Raises:
            OpsLeadRequired: The account is valid and is not an ops lead. 403, not 401 — signing in
                again cannot help.
        """
        if not user.is_ops_lead:
            raise OpsLeadRequired(OPS_LEAD_ACTION)


#: The API-wide default, set on the ``NinjaAPI`` instance so a new route is authenticated unless
#: someone deliberately writes ``auth=None``.
token_auth = TokenAuth()

#: The two-rule scheme, declared per route.
ops_lead = OpsLeadAuth()


def authenticate_request(request: HttpRequest) -> User:
    """Authenticate a request that ninja does not serve — that is, ``GET /api/stream``.

    The stream is a plain Django view because its response is an open-ended byte stream rather than
    a schema, so it cannot inherit the API's default ``auth``. This function is how it inherits the
    *rule* instead: same token, same cookie, same typed rejections, one implementation.

    Args:
        request: The incoming request.

    Returns:
        The authenticated person.

    Raises:
        AuthenticationRequired: No usable credential was presented.
        TokenRejected: A token was presented and refused.
    """
    return token_auth(request)


def actor_code_of(request: HttpRequest) -> str:
    """The ``accounts.User.code`` a service expects, for the authenticated caller.

    A named indirection so no router writes ``request.user.code`` and inherits a ``.code`` on
    ``AnonymousUser`` that does not exist. It is also the one place that narrows Django's
    ``request.user`` from ``AbstractBaseUser | AnonymousUser`` to the concrete model, so no router
    body carries a cast.

    Args:
        request: A request served by a route that did not opt out of authentication.

    Returns:
        The acting person's stable code, ready to pass into a service.

    Raises:
        TypeError: The route declared ``auth=None``, so nobody is signed in. A programming error
            rather than a client one — the route declaration is the fix — which is why it is not
            part of the HTTP contract.
    """
    user = request.user
    if not isinstance(user, User):
        message = "This route must be authenticated to read the actor; it declared auth=None."
        raise TypeError(message)
    return user.code


def set_access_cookie(response: HttpResponse, *, token: str) -> None:
    """Attach the access token as the cookie ``EventSource`` will send.

    ``HttpOnly`` so no script on the page can read it, which is what keeps an XSS from walking away
    with a usable credential; ``Secure`` outside local development, because a browser refuses a
    ``Secure`` cookie over plain HTTP and the compose stack serves ``http://localhost``;
    ``SameSite=Lax`` so it is not attached to cross-site form posts at all. Its ``max_age`` matches
    the token's own lifetime, so a stale cookie expires with the credential inside it rather than
    outliving it and producing a 401 the client cannot explain.

    Args:
        response: The response being built, injected into the route by ninja.
        token: The freshly minted access token.
    """
    response.set_cookie(
        ACCESS_COOKIE_NAME,
        token,
        max_age=int(api_settings.ACCESS_TOKEN_LIFETIME.total_seconds()),
        httponly=True,
        secure=settings.AUTH_COOKIE_SECURE,
        samesite=COOKIE_SAMESITE,
        path=ACCESS_COOKIE_PATH,
    )


def clear_access_cookie(response: HttpResponse) -> None:
    """Remove the access cookie. The same ``path`` is load-bearing.

    A ``delete_cookie`` with a different path than the ``set_cookie`` used silently deletes nothing:
    the browser treats them as two cookies and keeps the one that matters. That is why both sides
    read :data:`ACCESS_COOKIE_PATH` instead of spelling the path twice.

    Args:
        response: The logout response being built.
    """
    response.delete_cookie(
        ACCESS_COOKIE_NAME,
        path=ACCESS_COOKIE_PATH,
        samesite=COOKIE_SAMESITE,
    )


def _presented_token(request: HttpRequest) -> str | None:
    """The raw token this request offers, from the header first and the cookie second.

    Header first so a deliberate ``Authorization`` always wins: a developer curling with a token
    while a stale cookie sits in the jar must be testing the token they typed. The cookie is only
    consulted for a method that cannot change state — see the module docstring.

    Args:
        request: The incoming request.

    Returns:
        The raw JWT, or ``None`` when the request presented nothing usable.
    """
    header = request.headers.get("Authorization", "")
    if header:
        scheme, _, value = header.partition(" ")
        return value.strip() if scheme.lower() == BEARER_SCHEME and value.strip() else None
    if request.method in COOKIE_SAFE_METHODS:
        return request.COOKIES.get(ACCESS_COOKIE_NAME) or None
    return None
