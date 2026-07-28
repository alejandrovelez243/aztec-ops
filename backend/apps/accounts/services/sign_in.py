"""Use cases: exchange credentials for a token pair, and renew an access token.

The two write nothing and are still services rather than router bodies, for the reason every other
use case is: ``api/`` may not import ``models``, and the rule that keeps a router four lines long
is what stops the next person adding "just one query" to a view.

**No refresh-token rotation and no blacklist.** ``ninja_jwt.token_blacklist`` is deliberately not
installed: it is two tables and a write on every sign-in, and it buys server-side revocation that
this deployment has no way to trigger — there is no "sign out everywhere" surface and no incident
runbook that calls for one. What logout does instead is documented on the route: it clears the
cookie, which is the only copy of a credential the browser holds automatically. Rotating the
signing key revokes every token at once and is the escape hatch if that day comes.
"""

from django.contrib.auth import authenticate
from ninja_jwt.exceptions import TokenError
from ninja_jwt.settings import api_settings
from ninja_jwt.tokens import RefreshToken

from apps.accounts.domain.errors import InvalidCredentials, TokenRejected
from apps.accounts.domain.value_objects import AccessGrant, TokenPair
from apps.accounts.models import User


def sign_in(*, username: str, password: str) -> TokenPair:
    """Verify credentials and mint a fresh access/refresh pair.

    Delegates the password check to Django's configured authentication backends rather than
    comparing hashes here, which is what makes the account also work in ``/admin/`` and what keeps
    the hasher upgrade path (``PASSWORD_HASHERS``) a settings decision instead of a code one.
    Inactive accounts are refused by ``ModelBackend`` itself.

    Args:
        username: ``accounts.User.username``, which mirrors ``User.code``.
        password: The raw password, never stored and never logged.

    Returns:
        The pair, plus who it belongs to and whether they are an ops lead.

    Raises:
        InvalidCredentials: Unknown username, wrong password, or a deactivated account. One error
            for all three, so the endpoint cannot be used to enumerate accounts.
    """
    user = authenticate(username=username, password=password)
    if not isinstance(user, User):
        raise InvalidCredentials

    # ``for_user`` is annotated on the library's side with ``cls: T``, which mypy reads as an
    # invalid self argument on every call. The call is the documented constructor, so the check is
    # silenced here rather than the token being assembled by hand from private claim names.
    token = RefreshToken.for_user(user)  # type: ignore[misc]
    return TokenPair(
        access=str(token.access_token),
        refresh=str(token),
        expires_in=_access_lifetime_seconds(),
        actor=user.to_ref(),
        is_ops_lead=user.is_ops_lead,
    )


def refresh_access(*, refresh_token: str) -> AccessGrant:
    """Exchange a valid refresh token for a new access token.

    The account is re-read on every refresh rather than trusted from the token's claims. That is
    the whole reason short access tokens plus a long refresh token is worth the extra round trip:
    deactivating someone takes effect within one access-token lifetime instead of within the
    refresh window.

    Args:
        refresh_token: The ``refresh`` value returned by :func:`sign_in`.

    Returns:
        A new access token for the same person, with their current ops-lead status — a role change
        applies at the next refresh and does not need a new sign-in.

    Raises:
        TokenRejected: The token is malformed, expired, not a refresh token, or names an account
            that no longer exists or can no longer sign in.
    """
    try:
        token = RefreshToken(refresh_token)
    except TokenError as error:
        raise TokenRejected from error

    user_id = token.get(api_settings.USER_ID_CLAIM)
    if user_id is None:
        raise TokenRejected

    user = User.objects.with_role().active().filter(pk=user_id).first()
    if user is None:
        raise TokenRejected

    return AccessGrant(
        access=str(token.access_token),
        expires_in=_access_lifetime_seconds(),
        actor=user.to_ref(),
        is_ops_lead=user.is_ops_lead,
    )


def _access_lifetime_seconds() -> int:
    """How long a freshly minted access token is good for, in whole seconds.

    Read from ``NINJA_JWT`` rather than restated as a constant here, so the number the client
    schedules its refresh against and the number the token is actually signed with cannot drift.
    """
    return int(api_settings.ACCESS_TOKEN_LIFETIME.total_seconds())
