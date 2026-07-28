"""Resolving a client's ``order_by`` against a per-endpoint allowlist.

An allowlist rather than a passthrough, because the value reaches a database: accepting an
arbitrary field name is accepting an arbitrary join, and a name the ORM does not recognise raises
``FieldError``, which is a 500 for what is really a bad request (`docs/API.md` §1.4).

Silently ignoring an unknown name is the worse alternative and is not on offer: the client would
render a differently-ordered page than the one it believes it asked for, and nothing in the
response would say so.

Lives in the shared kernel because it is a read-parameter concern of every list endpoint and a
business rule of none — the *allowlists* belong to their contexts; this is only the parser.
"""

from collections.abc import Mapping

#: The prefix that flips a field to descending, as `docs/API.md` §1.4 fixes it.
DESCENDING_PREFIX = "-"


class UnknownOrdering(Exception):
    """``order_by`` named a field outside the endpoint's allowlist.

    Mapped to 422 by the API's central handler, and it carries the allowlist so the message can
    tell the client what it *may* sort by instead of only what it may not.
    """

    def __init__(self, field_name: str, allowed: tuple[str, ...]) -> None:
        super().__init__(f"Cannot order by {field_name!r}; allowed: {', '.join(sorted(allowed))}.")
        self.field_name = field_name
        self.allowed = allowed


def resolve_ordering(order_by: str, allowlist: Mapping[str, str], *, tiebreaker: str) -> list[str]:
    """Translate one signed wire field name into the ORM ordering to apply.

    ``tiebreaker`` is always appended. Without it, two rows tying on the requested field have no
    defined relative order, and PostgreSQL may return them differently on two pages of the same
    scan — which surfaces as one row appearing twice while another never appears at all.

    Args:
        order_by: A key of ``allowlist``, optionally prefixed with ``-`` for descending.
        allowlist: Wire field name to ORM field path, for this endpoint.
        tiebreaker: The stable final key, normally the business code.

    Returns:
        The arguments to hand to ``QuerySet.order_by``.

    Raises:
        UnknownOrdering: The field is outside the allowlist.
    """
    descending = order_by.startswith(DESCENDING_PREFIX)
    field_name = order_by.removeprefix(DESCENDING_PREFIX)
    column = allowlist.get(field_name)
    if column is None:
        raise UnknownOrdering(field_name, tuple(allowlist))
    return [f"{DESCENDING_PREFIX}{column}" if descending else column, tiebreaker]
