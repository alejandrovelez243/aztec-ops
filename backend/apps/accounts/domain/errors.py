"""Typed failures of actor resolution.

All three are 422 rather than 401 or 403, and that is deliberate. There is no authentication in
this scope (ARCHITECTURE §12): ``X-Actor`` is a *stand-in* that names who is acting, so a missing
or nonsensical value is a malformed request, not a rejected credential. Returning 401 would
promise a login flow that does not exist and would make the frontend render a sign-in prompt for
what is really a client bug.

When real authentication arrives these three disappear together with the header, replaced by the
session or token check — which is why nothing else in the codebase raises them.
"""


class ActorError(Exception):
    """Base class for every failure of the ``X-Actor`` stand-in.

    Registered once with the API's exception handler, so adding a case here never adds a
    ``try/except`` to a router.
    """


class ActorHeaderMissing(ActorError):
    """The request carried no ``X-Actor`` header, or carried it empty.

    Every mutating route requires one: an ``ActivityRecord`` with a blank actor cannot answer "who
    changed this", which is the question the whole audit trail exists for.
    """

    def __init__(self) -> None:
        super().__init__("The X-Actor header is required on every mutating request.")


class SystemActorRejected(ActorError):
    """An HTTP client claimed to be ``system``.

    ``system`` is reserved for the prioritization engine, the risk evaluator and the stream
    consumers (EVENTS.md §1). Letting a browser send it would make an operator's manual override
    indistinguishable from a policy recomputation in the timeline, which is the one distinction
    ``ActivityRecord.origin`` exists to preserve.
    """

    def __init__(self) -> None:
        super().__init__("'system' is written by consumers only and cannot be sent by a client.")
        self.actor_code = "system"


class ActorNotFound(ActorError):
    """The header named a code that matches nobody on the roster.

    Inactive people still resolve — retiring someone must not make their projects unmovable — so
    reaching this error always means the code itself is wrong.
    """

    def __init__(self, actor_code: str) -> None:
        super().__init__(f"No person with code {actor_code!r}.")
        self.actor_code = actor_code
