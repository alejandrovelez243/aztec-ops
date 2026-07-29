"""Typed prioritization errors.

Every failure the engine can produce is one of these, carrying the identifiers a caller needs to
render a message. They are mapped to HTTP once, centrally, on the ninja API — a service never
catches its own domain error to turn it into a response.
"""


class DomainError(Exception):
    """Base class for every prioritization failure.

    Exists so the API's central handler can catch one type and so no caller is ever tempted to
    catch bare ``Exception``.
    """


class ActivePolicyNotFound(DomainError):
    """Raised when no ``PriorityPolicy`` row has ``is_active``.

    The engine refuses to score with an implicit default: an unweighted score would silently
    rank the whole portfolio by nothing while looking exactly like a real one.
    """

    def __init__(self) -> None:
        super().__init__("No active PriorityPolicy; activate exactly one version before scoring.")


class PolicySignalMismatch(DomainError):
    """Raised at policy load when weights and the signal registry do not describe the same set.

    A weight with no registered strategy would be silently dropped and a registered strategy with
    no weight would silently contribute zero. Both make a persisted breakdown a lie about the
    policy that produced it, so loading fails instead.
    """

    def __init__(
        self, version: str, missing_strategies: tuple[str, ...], unweighted: tuple[str, ...]
    ) -> None:
        super().__init__(
            f"Policy {version} does not match the signal registry: "
            f"weights with no strategy {list(missing_strategies)}, "
            f"strategies with no weight {list(unweighted)}."
        )
        self.version = version
        self.missing_strategies = missing_strategies
        self.unweighted = unweighted


class PolicyWeightsNotNormalized(DomainError):
    """Raised at policy load when the weights do not sum to 1.0.

    The score's stated range is 0-100 and every reader assumes it. Weights summing to 0.9 would
    cap the portfolio at 90 and make two policy versions incomparable without anyone noticing.
    """

    def __init__(self, version: str, total: float) -> None:
        super().__init__(f"Policy {version} weights sum to {total}, expected 1.0.")
        self.version = version
        self.total = total


class SignalNotRegistered(DomainError):
    """Raised when a signal code is resolved and no strategy is registered under it."""

    def __init__(self, code: str) -> None:
        super().__init__(f"No signal strategy registered under code {code!r}.")
        self.code = code


class SignalAlreadyRegistered(DomainError):
    """Raised when two strategies claim the same code.

    The code is the key inside every persisted ``breakdown``; two strategies under one code would
    make historical scores unreadable depending on import order.
    """

    def __init__(self, code: str) -> None:
        super().__init__(f"A signal strategy is already registered under code {code!r}.")
        self.code = code


class RiskFlagAlreadyRegistered(DomainError):
    """Raised when two specifications claim the same flag code."""

    def __init__(self, flag_code: str) -> None:
        super().__init__(f"A specification is already registered under flag code {flag_code!r}.")
        self.flag_code = flag_code


class OverrideReasonRequired(DomainError):
    """Raised when an override is applied with an empty or whitespace-only reason.

    A forced position with no recorded reason is indistinguishable from a bug three weeks later,
    which is why the same rule also exists as a database check constraint.
    """

    def __init__(self, project_code: str) -> None:
        super().__init__(f"An override on {project_code} requires a non-empty reason.")
        self.project_code = project_code


class OverrideMechanismAmbiguous(DomainError):
    """Raised when an override sets neither or both of ``position`` and ``boost``.

    Exactly one mechanism per row: with both set, two readers of the queue would disagree about
    where the project actually sits.
    """

    def __init__(self, project_code: str) -> None:
        super().__init__(f"An override on {project_code} sets exactly one of position or boost.")
        self.project_code = project_code


class ProjectNotFound(DomainError):
    """Raised when a recomputation or an override names a project code that does not exist."""

    def __init__(self, project_code: str) -> None:
        super().__init__(f"No project with code {project_code!r}.")
        self.project_code = project_code
