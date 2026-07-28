"""Payload schema of the one topic this context emits (EVENTS.md §4).

Pure: no Django, no model, no envelope. The envelope is assembled by ``apps.events.services``; what
lives here is only the body, so a payload can be constructed and asserted on ``SimpleTestCase``
and so adding a topic never edits the events context.

``project.priority.recalculated`` is emitted by a registered handler rather than by an API route,
and **only when something actually moved**. That conditionality is a property of the caller, not of
this model — but it is the reason ``previous_value`` exists: an event that cannot state what it
replaced forces every reader to hold the prior value to know whether the new one is news. It is
also the reason the score is the one derived value this system still stores (ADR 0011): a computed
flag has no previous set to compare against, so it has no change event and travels in every project
payload instead.

Each payload knows how to be built from the engine's own value objects. The mapping lives here,
beside the schema it produces, because it is a projection of the same fact into the wire's units —
``Decimal`` to JSON number, ``detail`` to ``reason``. A handler that restated those field by field
would be a second, drifting opinion about what the browser is told.
"""

from decimal import Decimal
from typing import Final, Literal, Self

from pydantic import BaseModel, ConfigDict, Field

from .types import ScoreBreakdown

#: ``origin`` of a recalculated score: the engine computed it, or a live override is forcing the
#: project's position. Copied into the payload so the UI can label the row without a second query.
ScoreOrigin = Literal["POLICY", "MANUAL"]

ORIGIN_POLICY: Final[ScoreOrigin] = "POLICY"
ORIGIN_MANUAL: Final[ScoreOrigin] = "MANUAL"


class BreakdownLine(BaseModel):
    """One signal's contribution to a score, as the browser receives it.

    A restatement of ``SignalContribution`` with the decimals rendered as JSON numbers: the UI
    sorts by ``contribution`` to justify a rank, and pydantic's default quoted decimals would put
    ``"9.0"`` above ``"25.0"``.
    """

    model_config = ConfigDict(frozen=True)

    code: str
    raw: float = Field(ge=0.0, le=1.0)
    weight: float
    contribution: float
    reason: str = Field(min_length=1)


class PriorityRecalculatedPayload(BaseModel):
    """Body of ``project.priority.recalculated``.

    ``breakdown`` travels with the number rather than behind an endpoint because the queue reorders
    live and the reason is what makes the new position defensible at the moment it appears; a
    client that had to fetch the explanation would render an unexplained jump first.

    ``previous_value`` is ``None`` only on a project's first computation. On every later event it
    is populated, which is what lets an island animate the movement instead of replacing a number.
    """

    model_config = ConfigDict(frozen=True)

    value: float = Field(ge=0.0, le=100.0)
    previous_value: float | None = Field(ge=0.0, le=100.0)
    policy_version: str
    origin: ScoreOrigin
    breakdown: tuple[BreakdownLine, ...]
    modifiers: dict[str, float] = Field(default_factory=dict)
    flags: tuple[str, ...] = ()

    @classmethod
    def of(
        cls,
        *,
        breakdown: ScoreBreakdown,
        previous_value: Decimal | None,
        origin: ScoreOrigin,
    ) -> Self:
        """Build the payload from the breakdown that justifies the number.

        Args:
            breakdown: The explanation the engine just persisted. Its ``value``, ``policy_version``
                and ``flags`` are copied verbatim, so the event and the stored row can never
                disagree about why a project ranks where it does.
            previous_value: The score this one replaced, or ``None`` on a project's first
                computation. Carried so an island can animate a movement instead of replacing a
                number.
            origin: ``POLICY`` when the engine decided the rank, ``MANUAL`` when a live override
                is forcing it.

        Returns:
            The body of ``project.priority.recalculated``, with every decimal already a JSON
            number — quoted decimals would sort ``"9.0"`` above ``"25.0"`` in the queue.

        Raises:
            pydantic.ValidationError: The breakdown carries a value outside 0-100, which means the
                policy weights are not normalized and the number is not a rank at all.
        """
        return cls(
            value=float(breakdown.value),
            previous_value=float(previous_value) if previous_value is not None else None,
            policy_version=breakdown.policy_version,
            origin=origin,
            breakdown=tuple(
                BreakdownLine(
                    code=signal.code,
                    raw=signal.raw,
                    weight=float(signal.weight),
                    contribution=float(signal.contribution),
                    reason=signal.reason,
                )
                for signal in breakdown.signals
            ),
            modifiers={modifier.code: float(modifier.factor) for modifier in breakdown.modifiers},
            flags=breakdown.flags,
        )
