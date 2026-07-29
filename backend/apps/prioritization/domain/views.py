"""What the prioritization context publishes to a read surface: a score with its argument.

Every score is delivered with the breakdown that produced it (CLAUDE.md rule 7): the UI has to be
able to answer "why is this ranked first" from the payload alone, without a second call and
without recomputing anything. These are the shapes `docs/API.md` §1.6 fixes as ``Score``,
``ScoreSignal``, ``Override`` and ``RiskFlag``.

:class:`ScoreView` is built by parsing the persisted JSONB document rather than by re-running the
engine, which is the point: the number the client sees and the sentences justifying it come from
the same row, so they can never disagree. Signal codes, weights and ``policy_version`` are
explicitly *not* a closed set (`docs/API.md` §4.2) — the client renders whatever entries arrive.

Pure Pydantic, no Django: a consumer, a test or a router can construct one without a database.
"""

from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, model_validator

#: Keys of the breakdown document written by ``ScoreBreakdown.as_document`` (DATA_MODEL §6.3).
#: Named rather than inlined because this module *reads* a document another module writes, and a
#: silent typo here would surface as an empty breakdown rather than as an error.
_DOC_SIGNALS = "signals"
_DOC_MODIFIERS = "modifiers"
_DOC_FLAGS = "flags"
_DOC_COMPUTED_AT = "computed_at"


class ScoreSignalView(BaseModel):
    """One line of the argument: what the signal read, what it was worth, and why.

    ``contribution`` is ``raw * weight * 100`` as it was computed, not as the client should
    recompute it — floating point in a browser would put a row a hundredth of a point out of order
    against a server that used ``Decimal``.

    ``label`` is the Spanish name of what the signal measures and ``reason`` the Spanish sentence
    naming the fact it read. Both ship from the server because `docs/standards/FRONTEND.md` forbids
    any table in the frontend keyed by signal code — which is exactly what keeps a seventh signal a
    backend-only change. ``label`` is non-empty by validation; ``code`` stays on the wire as the
    stable identifier a client may key state on, never as something to render.
    """

    model_config = ConfigDict(frozen=True)

    code: str
    label: str = Field(min_length=1)
    raw: float
    weight: float
    contribution: float
    reason: str

    @model_validator(mode="before")
    @classmethod
    def _caption_a_row_written_before_labels(cls, data: object) -> object:
        """Fill in the label of a breakdown line persisted before the field existed.

        Labels are stored with the line (see
        :class:`~apps.prioritization.domain.types.SignalContribution`), so every document written
        from now on carries its own. A row written earlier carries none, and it must degrade to a
        readable caption rather than to a validation error that empties the whole breakdown — the
        queue would then show a score nobody can defend.

        Args:
            data: Whatever pydantic was handed; only a mapping is inspected, anything else is
                passed through for the normal validation to reject.

        Returns:
            The input unchanged when it already carries a label, otherwise a copy captioned from
            the registry — or from the code itself, when no strategy claims that code any more.
        """
        if not isinstance(data, dict) or data.get("label"):
            return data
        code = data.get("code")
        if not isinstance(code, str) or not code:
            return data
        return {**data, "label": _current_label(code)}


class ScoreView(BaseModel):
    """A project's computed rank, with the whole document that defends it.

    ``value`` is the computed 0-100 number and is **never** rewritten by an override: an override
    is stored beside it, so the queue can label a row as manually forced while still showing what
    the engine thought (ARCHITECTURE §4.2). A client that overwrites one with the other destroys
    the only signal that says the ranking was argued with.

    ``breakdown`` arrives sorted by ``contribution`` descending — the order the UI reads it in —
    and that ordering is the writer's, preserved here rather than re-sorted, so a policy that ever
    emits ties keeps a stable presentation.
    """

    model_config = ConfigDict(frozen=True)

    value: float
    policy_version: str
    computed_at: datetime | None = None
    breakdown: tuple[ScoreSignalView, ...] = ()
    modifiers: dict[str, float] = Field(default_factory=dict)
    flags: tuple[str, ...] = ()

    @classmethod
    def from_document(
        cls,
        document: object,
        *,
        value: Decimal,
        policy_version: str,
    ) -> "ScoreView":
        """Parse the persisted breakdown JSONB into the projection the API returns.

        ``value`` and ``policy_version`` are taken from their own columns rather than from the
        document, because those columns are what the queue ordered by and what the check
        constraint bounded; a document that disagreed with them would otherwise silently win.

        The document is read defensively — it is JSONB, and a hand-edited or pre-migration row must
        degrade to "no breakdown" instead of turning the whole queue into a 500. An empty
        breakdown is visible in the response and is the correct alarm.

        Args:
            document: ``PriorityScore.breakdown`` / ``ProjectSnapshot.breakdown``, as loaded.
            value: The persisted score, authoritative over the document's copy.
            policy_version: The persisted policy version, authoritative for the same reason.

        Returns:
            The score with whatever argument the document actually carried.
        """
        fields = document if isinstance(document, dict) else {}
        return cls(
            value=float(value),
            policy_version=policy_version,
            computed_at=_read_instant(fields.get(_DOC_COMPUTED_AT)),
            breakdown=_read_signals(fields.get(_DOC_SIGNALS)),
            modifiers=_read_modifiers(fields.get(_DOC_MODIFIERS)),
            flags=tuple(str(flag) for flag in _as_list(fields.get(_DOC_FLAGS))),
        )


class OverrideView(BaseModel):
    """A human's forced ranking decision, on the record.

    ``reason`` is not optional and never empty: it is a database constraint, a service check and a
    field here, because a forced position with no recorded justification is indistinguishable from
    a bug three weeks later. Exactly one of ``position`` and ``boost`` is set.
    """

    model_config = ConfigDict(frozen=True)

    position: int | None = None
    boost: float | None = None
    reason: str
    actor: str
    created_at: datetime
    expires_at: datetime | None = None


class RiskFlagView(BaseModel):
    """One raised risk, as the panels and the row indicators render it.

    ``code`` is deliberately an open set (`docs/API.md` §4.2): adding a specification adds a code
    (CLAUDE.md rule 8), so a client that does not recognise one renders it with its ``label`` and
    ``reason`` rather than dropping it — a dropped flag is a risk nobody sees.

    ``label`` is the Spanish chip text and ``reason`` the Spanish sentence naming the fact, both
    written by the specification that raised the flag. They travel on the wire because the frontend
    is forbidden from keeping an object literal keyed by a flag code
    (`docs/standards/FRONTEND.md` §7) — that map is what would make a seventh specification a
    frontend change. ``label`` is non-empty by validation; ``severity`` stays a bare code, because
    it is what the client branches on to pick a tone.
    """

    model_config = ConfigDict(frozen=True)

    code: str
    severity: str
    label: str = Field(min_length=1)
    reason: str = ""


def _current_label(code: str) -> str:
    """The registry's label for a signal code, falling back to the code itself.

    The import is local on purpose: ``registry`` imports ``types``, which imports this module for
    ``RiskFlagView``, so a module-level import here would close that cycle at startup. Nothing is
    resolved before the registry is fully populated — this runs only when a persisted document is
    read, long after app-ready.

    Args:
        code: The signal code the stored line carried.

    Returns:
        The registered strategy's label, or ``code`` when the strategy has since been retired. The
        code is the last resort and is visibly wrong in an interface, which is the point: it says a
        row predates the label and no strategy explains it any more.
    """
    from .registry import signal_label  # noqa: PLC0415

    return signal_label(code) or code


def _as_list(value: object) -> list[object]:
    """Read a JSONB array defensively; anything else reads as empty."""
    return value if isinstance(value, list) else []


def _read_instant(value: object) -> datetime | None:
    """Parse the document's ISO-8601 ``computed_at``; an unparseable value reads as absent."""
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _read_signals(value: object) -> tuple[ScoreSignalView, ...]:
    """Parse the ``signals`` array, skipping any entry that is not a complete line.

    A partial line is skipped rather than filled with zeros: a contribution of 0.0 with an empty
    reason reads as "this signal found nothing", which is a different and false statement.
    """
    lines: list[ScoreSignalView] = []
    for entry in _as_list(value):
        if not isinstance(entry, dict):
            continue
        try:
            lines.append(ScoreSignalView.model_validate(entry))
        except ValueError:
            continue
    return tuple(lines)


def _read_modifiers(value: object) -> dict[str, float]:
    """Flatten the document's ``[{code, factor, reason}]`` into the wire's ``{code: factor}``.

    The document keeps each modifier's reason for the audit trail; the wire carries only the factor
    because that is what the UI renders next to the score. Both readings come from one array, so a
    modifier can never be shown with the wrong factor.
    """
    factors: dict[str, float] = {}
    for entry in _as_list(value):
        if not isinstance(entry, dict):
            continue
        code = entry.get("code")
        factor = entry.get("factor")
        if isinstance(code, str) and isinstance(factor, int | float):
            factors[code] = float(factor)
    return factors
