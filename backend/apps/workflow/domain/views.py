"""What the workflow context publishes to a read surface: the legal moves out of a state.

This is the load-bearing projection of the whole product. `docs/API.md` §2.2 makes it the **only**
source of transition buttons: the frontend holds no list of state codes, guesses no legality and
renders exactly what arrives here, so adding ``en_espera_cliente`` from the admin is a row and
zero frontend changes (CLAUDE.md rule 14).

Pure Pydantic over the shared kernel — no Django — so a router, a consumer or a test can build one
without a database.
"""

from pydantic import BaseModel, ConfigDict

from apps.shared.refs import StateRef


class TransitionOption(BaseModel):
    """One edge an operator may take from where the aggregate currently sits.

    ``requires_reason`` and ``requires_fields`` are shipped rather than enforced client-side only:
    the client uses them to ask for the reason *before* the request and to disable a button whose
    field is still empty, and the transition service re-checks both, so a client that ignores them
    gets a typed 422 instead of a silent move.

    ``requires_fields`` names attributes of the aggregate (``next_step``), not of the request body.
    An entry that is currently empty on the aggregate means the button renders disabled with the
    field named — which is why the list is sent even when it changes nothing about the payload.
    """

    model_config = ConfigDict(frozen=True)

    to_state: StateRef
    label: str
    requires_reason: bool = False
    requires_fields: tuple[str, ...] = ()


def required_field_names(requires_fields: object) -> tuple[str, ...]:
    """Read ``WorkflowTransition.requires_fields`` defensively.

    The column is JSONB an operator edits as free-form JSON in the admin, so it can legitimately
    arrive as ``null``, as an object, or with blank entries. A non-list is read as "no required
    fields" rather than raising: a malformed edit must not make an otherwise legal transition
    impossible to take, and the reviewer sees the empty list in the API response.

    Lives here so the transition service and the read projection parse the column identically —
    a button rendered from one parser and validated by another is how a UI ends up offering a move
    the backend then refuses.

    Args:
        requires_fields: The raw JSONB value.

    Returns:
        The non-blank field names, in the order the operator wrote them.
    """
    if not isinstance(requires_fields, list):
        return ()
    return tuple(str(name) for name in requires_fields if str(name).strip())
