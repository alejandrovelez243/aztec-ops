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


class TaxonomyRowNotFound(UnknownCode):
    """A taxonomy row was addressed by code, for editing, and does not exist.

    A subclass of :class:`UnknownCode` rather than a sibling, because it *is* one — the code did
    not resolve. What differs is who is asking and what they should be told. When the engine fails
    to resolve a code it read from a project, the contract between the code base and the taxonomy
    is broken and 422 is the honest answer; when a client sends ``PATCH .../roles/design`` for a
    role somebody retired and deleted, the resource it addressed is simply not there, and 404 is
    what tells its form to stop showing a row that no longer exists.

    Attributes:
        taxonomy: Human name of the taxonomy that was searched.
        code: The slug that did not resolve.
    """


class DuplicateTaxonomyCode(CatalogError):
    """A taxonomy value was created under a code that already exists.

    A conflict rather than a validation failure: the request is well-formed and the operator has
    to pick a different slug. It is also the answer for a **retired** row carrying that code — the
    row still exists, it is merely out of the pickers, so the fix is to restore it rather than to
    create a second one that would resolve ambiguously ever after.

    Attributes:
        taxonomy: Human name of the taxonomy, e.g. ``"role"``.
        code: The slug that is already taken.
    """

    def __init__(self, taxonomy: str, code: str) -> None:
        super().__init__(f"A {taxonomy} already exists with code {code!r}.")
        self.taxonomy = taxonomy
        self.code = code
