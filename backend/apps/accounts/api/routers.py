"""Routes of the identity context: sign in, renew, sign out, and who is on the roster.

The first two are the only routes on the API besides the health probes that declare ``auth=None``,
and they have to: a caller with no token is exactly who they are for. The exception list is stated
once, in :mod:`config.api`, so adding a fourth unauthenticated route is a visible decision rather
than a quiet ``auth=None``.

The three authentication routes touch the access cookie, and that is the only reason they take
ninja's injected ``response``: the cookie is what ``EventSource`` will send to ``GET /api/stream``,
which cannot carry an ``Authorization`` header (:mod:`config.auth`). The body carries the same token
for ``fetch`` and for ``curl``.

The roster routes are all ``auth=roster_admin``: reading who is on the team is everybody's business
and is served by ``GET /api/v1/team/load``, but changing it is not. They live under ``/team/`` even
though the aggregate is ``accounts.User``, because the URL space names the product's surface and
not the app that owns the row — the same reason ``work`` serves paths under ``/projects/{code}/``.

Every handler here is the same four lines as everywhere else: read the actor, build a command, call
one service, return what it gave back. No ``try/except`` — :mod:`config.errors` maps every typed
rejection once — and ``now`` and ``correlation_id`` are minted here so a service stays
deterministic and every record one request produces reconstructs as one decision.
"""

from uuid import uuid4

from django.http import HttpRequest, HttpResponse
from django.utils import timezone
from ninja import Router
from ninja.responses import Status

from apps.accounts.api.schemas import (
    CredentialsIn,
    MemberCreateIn,
    MemberPasswordIn,
    MemberUpdateIn,
    RefreshIn,
)
from apps.accounts.domain.commands import (
    CreateMemberCommand,
    SetMemberPasswordCommand,
    UpdateMemberCommand,
)
from apps.accounts.domain.value_objects import AccessGrant, TokenPair
from apps.accounts.domain.views import MemberView
from apps.accounts.services import (
    create_member,
    refresh_access,
    set_member_password,
    sign_in,
    update_member,
)
from config.auth import actor_code_of, clear_access_cookie, roster_admin, set_access_cookie

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


@router.post(
    "/team/members",
    response={201: MemberView},
    auth=roster_admin,
    url_name="member_create",
    tags=["team"],
)
def post_member(request: HttpRequest, payload: MemberCreateIn) -> Status[MemberView]:
    """Register a person on the roster.

    ``code`` is **not** accepted: it is derived from ``label`` — ``"Alejandro Vélez"`` becomes
    ``alejandro.velez``, a second person of that name ``alejandro.velez2`` — and allocated inside
    the creating transaction, so a rollback frees it. The slug is permanent and there is no rename,
    which is precisely why an operator does not get to typo one into a form. The response carries
    the code that was assigned.

    Omitting ``password`` is normal — the person is immediately assignable and cannot sign in until
    a credential is set through ``POST /team/members/{code}/password``.

    Errors: ``422 validation_error`` (unknown ``role``, a capacity outside the accepted range, a
    password the configured validators refuse), ``403 permission_denied``.
    """
    return Status(
        201,
        create_member(
            CreateMemberCommand(
                label=payload.label,
                role_code=payload.role,
                weekly_capacity_points=payload.weekly_capacity_points,
                password=payload.password,
            ),
            actor=actor_code_of(request),
            correlation_id=uuid4(),
            now=timezone.now(),
        ),
    )


@router.patch(
    "/team/members/{member_code}",
    response=MemberView,
    auth=roster_admin,
    url_name="member_update",
    tags=["team"],
)
def patch_member(request: HttpRequest, member_code: str, payload: MemberUpdateIn) -> MemberView:
    """Edit a person's mutable attributes. Absent means untouched; explicit ``null`` clears.

    Also the restore half of the retire/restore pair: ``is_active: true`` puts somebody back on the
    assignable roster. Retiring can go through here too, and ``DELETE`` is the same operation under
    the verb an operator expects.

    Errors: ``404 not_found``, ``422 validation_error`` (unknown ``role``, capacity out of range),
    ``403 permission_denied`` — including the refusal to deactivate your own account, which no
    ops lead may do to themselves.
    """
    return update_member(
        _update_command(member_code=member_code, payload=payload),
        actor=actor_code_of(request),
        correlation_id=uuid4(),
        now=timezone.now(),
    )


@router.delete(
    "/team/members/{member_code}",
    response={204: None},
    auth=roster_admin,
    url_name="member_deactivate",
    tags=["team"],
)
def delete_member(request: HttpRequest, member_code: str) -> Status[None]:
    """Retire a person from the operation, deleting nothing.

    ``DELETE`` because that is the verb an operator reaches for and the one a CRUD client offers,
    but the effect is ``is_active = false``: the tasks they are assigned and the projects they own
    still name them, and a row removed underneath those would either cascade the history away or
    break the foreign keys holding it. ``PATCH`` with ``is_active: true`` restores them.

    Idempotent — retiring somebody already retired is the same 204, so a retry after a dropped
    response is safe — and it writes no second activity record, because nothing changed.

    Errors: ``404 not_found``, ``403 permission_denied`` (not an ops lead, or the caller is the
    subject: retiring yourself ends your own session and, if you were the last ops lead, locks the
    product for everybody).
    """
    update_member(
        UpdateMemberCommand(code=member_code, is_active=False),
        actor=actor_code_of(request),
        correlation_id=uuid4(),
        now=timezone.now(),
    )
    return Status(204, None)


@router.post(
    "/team/members/{member_code}/password",
    response=MemberView,
    auth=roster_admin,
    url_name="member_password_set",
    tags=["team"],
)
def post_member_password(
    request: HttpRequest, member_code: str, payload: MemberPasswordIn
) -> MemberView:
    """Replace a person's password on their behalf.

    An administrative reset, so no current password is asked for — the caller is not the person.
    It is also how a seeded account, which starts with an unusable hash, becomes able to sign in at
    all; the returned ``has_password`` is the confirmation.

    The password is never echoed, logged, or published: no event is emitted for this route, and the
    activity record states that the credential was replaced and by whom, never with what.

    Errors: ``404 not_found``, ``422 validation_error`` naming ``password`` with every rule the
    proposed value broke, ``403 permission_denied``.
    """
    return set_member_password(
        SetMemberPasswordCommand(code=member_code, password=payload.password),
        actor=actor_code_of(request),
        correlation_id=uuid4(),
        now=timezone.now(),
    )


def _update_command(*, member_code: str, payload: MemberUpdateIn) -> UpdateMemberCommand:
    """Carry the caller's absent-versus-null distinction from the payload into the command.

    Only the fields the client actually sent are copied. Building the command from every attribute
    would turn "do not touch the role" into "unclassify this person", because both arrive as
    ``None`` — which is precisely the bug ``model_fields_set`` exists to prevent, and why this is a
    translation rather than a ``model_dump()``.
    """
    fields = {
        ("role_code" if name == "role" else name): value
        for name, value in payload.model_dump(exclude_unset=True).items()
    }
    return UpdateMemberCommand(code=member_code, **fields)
