"""The ``X-Actor`` header, as a ninja security scheme.

**Deliberate stand-in, not an oversight.** ARCHITECTURE §12 puts real authentication out of scope;
`docs/API.md` §1.2 fixes the substitute: every mutating request carries ``X-Actor: <User.code>``,
this class resolves it, and the router passes the resolved code into the service. It is declared as
a security scheme rather than as middleware so the OpenAPI document says which routes require it —
the generated client then knows the header is mandatory instead of discovering it as a 422.

What it is *not*: authentication. Nothing is verified. Anyone can claim to be ``camila``. It exists
so that every ``ActivityRecord`` and every event envelope has a real author (CLAUDE.md rule 3),
which is what makes the timeline answer "who changed this".

**What replaces it.** A real authentication backend populating ``request.user``, at which point
:func:`apps.accounts.services.resolve_actor` becomes ``request.user.code`` and the three failures
in ``apps.accounts.domain.errors`` collapse into one 401. Nothing else moves: no service reads a
request object, no router body names the header, and no use-case test constructs one — the
substitution is confined to this module and that service, on purpose.

The ``system`` rejection survives that change. It is not about authentication at all: it keeps an
engine-authored fact distinguishable from a human one, which is the distinction
``ActivityRecord.origin`` exists to preserve.
"""

from django.http import HttpRequest
from ninja.security import APIKeyHeader

from apps.accounts.services import resolve_actor
from apps.shared.refs import ActorRef

#: The header name. One constant, read by the security scheme, the OpenAPI document and the CORS
#: allowlist in settings, so the three cannot disagree about its spelling.
ACTOR_HEADER = "X-Actor"


class ActorHeader(APIKeyHeader):
    """Resolves ``X-Actor`` to a person, or rejects the request with a typed error.

    Ninja routes an exception raised here through the API's own handlers, so a missing, reserved or
    unknown actor produces the same ``{code, message, details}`` envelope as any domain rejection
    (`docs/API.md` §1.5) — a 401 with a different body would be a second shape the client has to
    parse for what is really a malformed request.
    """

    param_name = ACTOR_HEADER

    def authenticate(self, request: HttpRequest, key: str | None) -> ActorRef:
        """Resolve the header, or raise.

        Never returns ``None``: ninja treats a ``None`` result as "this scheme did not apply" and
        falls through to its generic 401, which would bypass the error envelope entirely. Every
        failure here is an exception instead.

        Args:
            request: The incoming request. Unused — resolution depends on the header alone, and
                a service that needed more of the request would be a service reading HTTP.
            key: The raw header value, or ``None`` when it was absent.

        Returns:
            The resolved person.

        Raises:
            ActorHeaderMissing: The header was absent or blank.
            SystemActorRejected: The header claimed the reserved ``system`` actor.
            ActorNotFound: The code matches nobody on the roster.
        """
        del request
        return resolve_actor(key)


#: The single instance every mutating route declares as ``auth=``. Read routes pass ``auth=None``:
#: the queue, the detail view and the stream are not actor-scoped, and requiring a header to read
#: them would make a server-rendered Astro page carry one for nothing.
actor_header = ActorHeader()


def actor_of(request: HttpRequest) -> ActorRef:
    """The person this request claims to be, as resolved by :class:`ActorHeader`.

    Ninja stores the authentication result on ``request.auth`` as ``Any``. This function is the one
    place that narrows it, so no router body carries a cast and no router can read ``request.auth``
    on a route that declared no ``auth=`` — where it would be ``None`` and fail at the first
    attribute access rather than at the boundary.

    Args:
        request: A request handled by a route declaring ``auth=actor_header``.

    Returns:
        The resolved person.

    Raises:
        TypeError: The route did not declare the actor scheme, so ``request.auth`` is not an
            actor. A programming error rather than a client one, which is why it is not part of
            the HTTP contract and surfaces as a 500 — the route declaration is the fix.
    """
    actor = getattr(request, "auth", None)
    if not isinstance(actor, ActorRef):  # pragma: no cover - guarded by the route declaration
        message = "This route must declare auth=actor_header to read the actor."
        raise TypeError(message)
    return actor


def actor_code_of(request: HttpRequest) -> str:
    """The ``accounts.User.code`` a service expects, for the person this request claims to be.

    A named indirection so no router writes ``actor_of(request).alias`` and leaves the next reader
    wondering why the *alias* identifies a person. It is the code; the wire calls the field
    ``alias`` (`docs/API.md` §1.6), and this is the one place the two names are reconciled.

    Args:
        request: A request handled by a route declaring ``auth=actor_header``.

    Returns:
        The acting person's stable code, ready to pass into a service.
    """
    return actor_of(request).alias
