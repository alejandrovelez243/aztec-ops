"""What a successful sign-in and a successful refresh hand back.

Two types rather than one with a nullable ``refresh``, because a refresh response that carried a
``refresh: null`` field would invite a client to overwrite the refresh token it still holds with
nothing (CLAUDE.md rule 13). Obtaining a pair and renewing an access token are different facts and
they are different shapes.

Both carry the actor and the ops-lead bit alongside the token. The frontend needs to know who it
is signed in as and whether the priority-override control should render at all, and a token whose
claims the client would have to decode itself is a token the client starts parsing — which is how
a frontend ends up trusting an unverified payload. The API says it instead.
"""

from pydantic import BaseModel, ConfigDict

from apps.shared.refs import ActorRef


class AccessGrant(BaseModel):
    """One access token and everything the client needs to use it.

    ``expires_in`` is seconds, not an absolute instant, so a client with a skewed clock still
    schedules its refresh correctly.
    """

    model_config = ConfigDict(frozen=True)

    access: str
    expires_in: int
    actor: ActorRef
    is_ops_lead: bool


class TokenPair(AccessGrant):
    """An access token, its refresh token, and who they belong to.

    Returned only by sign-in. The refresh token is *not* set as a cookie: it is the long-lived
    credential, it is never needed by ``EventSource``, and putting it in the browser's automatic
    request path would extend the blast radius of the one attack the access cookie's short lifetime
    is there to bound.
    """

    refresh: str
