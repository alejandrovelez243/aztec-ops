"""Typed failures of the identity context: authenticating a caller, and editing the roster.

Two trees under one root, because they are two different conversations. The authentication half is
about *this* request's credential and the split between its four cases is what tells the client
what to do next: no credential at all means "show the sign-in form"; a token the API refuses means
"try the refresh endpoint before giving up"; wrong username or password means "the form was
answered incorrectly"; a refusal on a privileged action means "this account is signed in and still
may not do this", which signing in again never fixes.

The roster half is about *somebody else's* row and is ordinary 404/409/422 territory. It is a
separate subtree rather than four more ``AuthError`` subclasses because a client catching "the
credential is the problem" must not also catch "the capacity you typed is not a number" — the two
have opposite recoveries, and only the exception hierarchy can keep them apart.

:mod:`config.errors` registers the root, so adding a class below never edits the API's error table
unless the new case deserves a status of its own.

``domain/`` is pure, so nothing here knows about HTTP.
"""


class AccountsError(Exception):
    """Base class for every failure of the identity context.

    The one class :mod:`config.errors` registers for this app. Both subtrees hang off it so a new
    error is a typed rejection by default rather than a 500.
    """


class AuthError(AccountsError):
    """Base class for every authentication and authorization failure.

    Registered once with the API's exception handler, so adding a case below never adds a
    ``try/except`` to a router.
    """


class MemberError(AccountsError):
    """Base class for every rejection of a write against the roster.

    Separate from :class:`AuthError` on purpose: these are refusals about the *subject* of the
    request, not about the caller. A frontend that treated them alike would sign an operator out
    because they typed a duplicate code.
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


class MemberNotFound(MemberError):
    """The addressed person does not exist.

    Carries ``person_code`` rather than a primary key, matching every other ``*NotFound`` in the
    codebase: the API addresses people by their stable code, so that is what the 404 can name.
    """

    def __init__(self, person_code: str) -> None:
        super().__init__(f"No person is registered under the code {person_code!r}.")
        self.person_code = person_code


class DuplicateMemberCode(MemberError):
    """The requested code already belongs to somebody.

    A conflict rather than a validation failure: the request is well-formed and the operator has
    to pick a different code, which is a different fix from correcting a malformed one. The code is
    also the ``username``, so this is the single answer for both collisions — they cannot happen
    independently, because :class:`~apps.accounts.models.User` keeps the two in step.
    """

    def __init__(self, person_code: str) -> None:
        super().__init__(f"The code {person_code!r} is already taken.")
        self.person_code = person_code


class RoleNotFound(MemberError):
    """The requested role code matches no ``catalog.Role`` row.

    A 422 naming ``role``, not a 404: the *person* is not what could not be found, and answering
    404 would tell a form that the row it is editing has disappeared.
    """

    def __init__(self, role_code: str) -> None:
        super().__init__(f"No role is registered under the code {role_code!r}.")
        self.role_code = role_code


class PasswordRejected(MemberError):
    """The proposed password did not survive Django's configured validators.

    Carries every complaint rather than the first, because a form that fixes one rule at a time
    across four round trips is a form people work around by choosing something worse.
    """

    def __init__(self, problems: tuple[str, ...]) -> None:
        super().__init__(" ".join(problems) or "The password was rejected.")
        self.problems = problems


class CannotDeactivateSelf(MemberError):
    """An operator attempted to retire their own account.

    Refused because the effect is immediate and self-inflicted: the next token refresh fails, the
    session ends, and if that operator was the last ops lead nobody can undo it from the product at
    all — recovery would mean a shell on the database. Retiring somebody is somebody else's action.
    """

    def __init__(self, person_code: str) -> None:
        super().__init__("You cannot deactivate your own account; ask another ops lead.")
        self.person_code = person_code
