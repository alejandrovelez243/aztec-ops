"""Typed errors raised by the catalog context.

Every error descends from :class:`CatalogError` so the central HTTP handler can map the whole
context in one clause instead of enumerating exception types per route.
"""


class CatalogError(Exception):
    """Base class for every failure the catalog context raises.

    Existing purely so callers can catch the context rather than a list of classes; it is never
    raised directly, because an error without a name tells the operator nothing.
    """


class UnknownCode(CatalogError):
    """A taxonomy code was requested and no row carries it.

    Logic compares against ``code``, so a code that does not resolve is a broken contract between
    the code base and the taxonomy table — a stale fixture, a typo in a payload, or a row deleted
    instead of deactivated. Failing loudly here is deliberate: returning ``None`` would let the
    caller carry a missing engagement type all the way into a score, where it becomes a wrong
    number instead of an error.

    Attributes:
        taxonomy: Human name of the taxonomy that was searched, e.g. ``"engagement type"``.
        code: The slug that did not resolve.
    """

    def __init__(self, taxonomy: str, code: str) -> None:
        super().__init__(f"No {taxonomy} exists with code {code!r}.")
        self.taxonomy = taxonomy
        self.code = code
