"""Loading and validating a ``PriorityPolicy`` row into a value the engine can use.

Postgres cannot know which strategies are registered in this Python process, so the agreement
between ``weights`` and the registry is checked here, once, at load. Loading raises rather than
defaulting a missing weight to zero: a policy that silently drops a signal produces scores that
look exactly like real ones and cannot be told apart afterwards.
"""

from decimal import Decimal
from typing import Final

from pydantic import BaseModel, ConfigDict

from .errors import PolicySignalMismatch, PolicyWeightsNotNormalized
from .registry import registered_signals

#: Tolerance on the weight sum. JSONB stores the weights as floats, so 0.25 + 0.20 + 0.15 * 3 +
#: 0.10 does not land exactly on 1.0 in binary floating point.
WEIGHT_SUM_TOLERANCE: Final[float] = 1e-9

#: Modifier key that multiplies the weighted sum by ``EngagementType.weight``.
ENGAGEMENT_TYPE_MODIFIER: Final[str] = "engagement_type"


class PolicyView(BaseModel):
    """A validated, immutable view of the active policy.

    Constructed only through :func:`load_policy`, so an instance of this type is proof that its
    weights cover the registry exactly and sum to 1.0.
    """

    model_config = ConfigDict(frozen=True)

    version: str
    weights: dict[str, Decimal]
    modifiers: dict[str, bool]

    def weight_of(self, signal_code: str) -> Decimal:
        """The weight this policy assigns to one signal.

        Args:
            signal_code: A registered signal code; validated to exist at load time.

        Returns:
            The weight, as a ``Decimal`` so the contribution arithmetic is exact.
        """
        return self.weights[signal_code]

    def applies_engagement_type_modifier(self) -> bool:
        """Whether this policy multiplies the weighted sum by the engagement type weight."""
        return self.modifiers.get(ENGAGEMENT_TYPE_MODIFIER, False)


def load_policy(
    *, version: str, weights: dict[str, float], modifiers: dict[str, bool]
) -> PolicyView:
    """Validate a persisted policy row against the signal registry.

    Args:
        version: ``PriorityPolicy.version``, copied into every score computed with it.
        weights: The ``weights`` JSONB, keyed by registered signal code.
        modifiers: The ``modifiers`` JSONB, e.g. ``{"engagement_type": true}``.

    Returns:
        A frozen view whose weights are known to cover every registered signal and to sum to 1.0.

    Raises:
        PolicySignalMismatch: A weight names an unregistered signal, or a registered signal has no
            weight. Either way the persisted breakdown would misdescribe the policy.
        PolicyWeightsNotNormalized: The weights do not sum to 1.0, which would silently move the
            top of the 0-100 range and make two policy versions incomparable.
    """
    registered = set(registered_signals())
    weighted = set(weights)

    missing_strategies = tuple(sorted(weighted - registered))
    unweighted = tuple(sorted(registered - weighted))
    if missing_strategies or unweighted:
        raise PolicySignalMismatch(version, missing_strategies, unweighted)

    total = sum(weights.values())
    if abs(total - 1.0) > WEIGHT_SUM_TOLERANCE:
        raise PolicyWeightsNotNormalized(version, total)

    return PolicyView(
        version=version,
        weights={code: Decimal(str(weight)) for code, weight in weights.items()},
        modifiers=dict(modifiers),
    )
