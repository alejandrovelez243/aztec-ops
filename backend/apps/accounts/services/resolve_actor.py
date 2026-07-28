"""Use case: turn the ``X-Actor`` header into a person the write services can name.

**This is a deliberate stand-in for authentication, not a shortcut that was forgotten.**
ARCHITECTURE §12 puts real authentication out of scope; `docs/API.md` §1.2 fixes the substitute on
the wire: every mutating request carries ``X-Actor: <User.code>``, the API resolves it here, and the
router passes the resolved code explicitly into the service. Services never read a request object,
so the day the header is replaced by a session, a JWT or an mTLS subject, **only this module and
the auth class in ``config/actor.py`` change** — no service, no router body and no test of a use
case touches the substitution.

What replaces it: a real authentication backend populating ``request.user``, and this function
becoming ``request.user.code`` with the same three failures collapsing into one 401. The
``system`` rejection survives that change, because it is not about authentication at all — it is
about keeping engine-authored facts distinguishable from human ones.
"""

from apps.accounts.domain.errors import ActorHeaderMissing, ActorNotFound, SystemActorRejected
from apps.accounts.models import User
from apps.shared.refs import SYSTEM_ACTOR_ALIAS, ActorRef


def resolve_actor(actor_code: str | None) -> ActorRef:
    """Resolve the header value to a person on the roster.

    Inactive people resolve on purpose: ``User.objects.by_code`` does not filter ``is_active``,
    because retiring somebody must not make every project they own unmovable. What is refused is a
    code nobody carries, an absent header, and the reserved ``system`` actor.

    Args:
        actor_code: Raw ``X-Actor`` header value, or ``None`` when the header was absent.

    Returns:
        The person as the reference shape every payload renders, whose ``alias`` is the
        ``User.code`` the router hands on to the service.

    Raises:
        ActorHeaderMissing: The header was absent, empty or whitespace only.
        SystemActorRejected: The header claimed the reserved ``system`` actor.
        ActorNotFound: The code matches nobody on the roster.
    """
    if actor_code is None or not actor_code.strip():
        raise ActorHeaderMissing

    code = actor_code.strip()
    if code == SYSTEM_ACTOR_ALIAS:
        raise SystemActorRejected

    user = User.objects.with_role().by_code(code).first()
    if user is None:
        raise ActorNotFound(code)
    return user.to_ref()
