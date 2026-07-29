"""Typed failures of authentication and of the two authorization rules.

Four errors, and the split between them is the point: the client does something different for
each. No credential at all means "show the sign-in form"; a token the API refuses means "try the
refresh endpoint before giving up"; wrong username or password means "the form was answered
incorrectly"; a refusal on an ops-lead action means "this account is signed in and still may not
do this", which signing in again never fixes.

They are 401/403 rather than the 422 the ``X-Actor`` stand-in returned, because there is now a
credential to reject. The 401 is a real promise: the token endpoints exist, so a frontend that
renders a sign-in prompt on one is responding to something true.

``domain/`` is pure, so nothing here knows about HTTP. :mod:`config.errors` owns the mapping from
each class to its status and its wire ``code``.
"""


class AuthError(Exception):
    """Base class for every authentication and authorization failure.

    Registered once with the API's exception handler, so adding a case below never adds a
    ``try/except`` to a router.
    """


class AuthenticationRequired(AuthError):
    """The request carried no usable credential at all.

    Raised before any token is parsed: there was no ``Authorization: Bearer`` header, and either
    the access cookie was absent or the request was a mutation — which the cookie is deliberately
    not accepted for (:mod:`config.auth`).
    """

    def __init__(self) -> None:
        super().__init__(
            "Authentication is required. Send 'Authorization: Bearer <access token>', "
            "or sign in at POST /api/v1/auth/token."
        )


class TokenRejected(AuthError):
    """A token was presented and the API refused it.

    One class for expired, malformed, wrongly typed and issued-for-a-deactivated-account, on
    purpose: the client's move is the same in every case — refresh, then sign in — and telling an
    unauthenticated caller *which* of those was wrong is free reconnaissance for whoever is
    guessing.
    """

    def __init__(self) -> None:
        super().__init__(
            "The token is invalid, expired, or issued for an account that can no longer sign in."
        )


class InvalidCredentials(AuthError):
    """Sign-in failed: unknown username, wrong password, or a deactivated account.

    Deliberately one error for all three. Distinguishing "no such user" from "wrong password"
    turns the sign-in form into an account-enumeration oracle, and an operator gains nothing from
    the distinction that retyping the password does not already give them.
    """

    def __init__(self) -> None:
        super().__init__("Username or password is incorrect, or the account is inactive.")


class OpsLeadRequired(AuthError):
    """An authenticated member attempted one of the two ops-lead actions.

    Not a hint to sign in again: the caller *is* authenticated, the resource exists, and the answer
    is still no. Carries the action so the response can name it — a bare "forbidden" sends an
    operator to read the source to find out what was refused.
    """

    def __init__(self, action: str) -> None:
        super().__init__(f"{action} is reserved for an ops lead.")
        self.action = action
