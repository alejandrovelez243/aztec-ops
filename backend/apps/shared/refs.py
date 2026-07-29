"""The three reference shapes every read surface of the API renders.

`docs/API.md` §1.6 fixes them on the wire: a taxonomy value, a workflow state and a person are
always delivered as ``{code, label, color}`` / ``{code, label, category, color}`` /
``{alias, label, role}``. The client renders ``label`` and ``color`` and compares only ``code`` and
``category`` (CLAUDE.md rule 1), which is the whole reason a bare string is never sent: a caller
handed ``"ejecucion"`` has to invent a label, and a caller handed ``"En ejecucion"`` starts
comparing against operator-editable Spanish.

They live in the shared kernel rather than in a context because all four read contexts need the
same three shapes and none of them owns all three: a task carries a priority (``catalog``), a state
(``workflow``) and an assignee (``accounts``).
"""

from typing import Self

from pydantic import BaseModel, ConfigDict

#: ``ActorRef`` for a change no person made. The bus writes ``system`` as the actor of every
#: engine-caused fact (EVENTS.md §1), and the timeline has to render it like any other author.
SYSTEM_ACTOR_ALIAS = "system"


class TaxonomyRef(BaseModel):
    """One operator-editable catalog value, ready to render.

    ``color`` is ``None`` rather than ``""`` when the operator set none: absence is a signal the
    client must be able to see, so it can fall back to its own neutral token instead of painting a
    swatch of transparent black.
    """

    model_config = ConfigDict(frozen=True)

    code: str
    label: str
    color: str | None = None

    @classmethod
    def of(cls, *, code: str, label: str, color: str = "") -> Self:
        """Build a reference from the raw column values of a taxonomy row.

        Args:
            code: Stable slug; the only thing logic compares against.
            label: Operator-editable display text.
            color: Hex triplet, or the empty string the column defaults to.

        Returns:
            The reference, with an unset color normalized to ``None``.
        """
        return cls(code=code, label=label, color=color or None)


class StateRef(BaseModel):
    """One workflow state, ready to render and safe to branch on.

    ``category`` travels alongside ``code`` because ``code`` is unique only inside its workflow —
    two workflows may both own ``bloqueada`` — so a client deciding "is this blocked" reads
    ``category`` and never a list of codes it would have to keep in sync (DATA_MODEL §12).
    """

    model_config = ConfigDict(frozen=True)

    code: str
    label: str
    category: str
    color: str | None = None

    @classmethod
    def of(cls, *, code: str, label: str, category: str, color: str = "") -> Self:
        """Build a state reference from the raw column values of a ``WorkflowState`` row.

        Args:
            code: State slug, unique within its workflow only.
            label: Operator-editable display text.
            category: ``BACKLOG`` | ``IN_PROGRESS`` | ``BLOCKED`` | ``DONE`` | ``CANCELLED``.
            color: Hex triplet, or the empty string the column defaults to.

        Returns:
            The reference, with an unset color normalized to ``None``.
        """
        return cls(code=code, label=label, category=category, color=color or None)


class ActorRef(BaseModel):
    """One person, as every payload that names a person renders them.

    ``alias`` is ``accounts.User.code`` — the stable slug the API accepts back as input — and
    ``label`` is ``accounts.User.alias``, the display name. The two names are inverted with
    respect to the model's columns because that is what `docs/API.md` §1.6 fixed on the wire, and
    the wire is the contract the frontend is generated from.
    """

    model_config = ConfigDict(frozen=True)

    alias: str
    label: str
    role: str | None = None

    @classmethod
    def of(cls, *, code: str, label: str, role: str | None = None) -> Self:
        """Build a person reference from the raw column values of a ``User`` row.

        Args:
            code: ``User.code``, the slug the API accepts as input.
            label: ``User.alias``, the display name.
            role: ``Role.label``, or ``None`` when the person is unclassified.

        Returns:
            The reference.
        """
        return cls(alias=code, label=label, role=role)

    @classmethod
    def system(cls) -> Self:
        """The stand-in author of every engine-caused fact.

        Returned instead of ``None`` so a timeline row always has an author to render; "nobody did
        this" and "the engine did this" are different facts and only one of them is true here.
        """
        return cls(alias=SYSTEM_ACTOR_ALIAS, label="System", role=None)
