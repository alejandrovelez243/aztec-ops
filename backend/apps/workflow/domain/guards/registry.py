"""The guard registry: a named callable resolved from `WorkflowTransition.guard`.

Same shape as the prioritization signal registry, for the same reason: a new guard is one
function plus one decorator line, and the transition service never learns a guard's name. If
adding a guard required editing an `if` in the service, the design would be wrong (CLAUDE.md
rule 8).

The key space is fixed and small, and an unknown key raises at resolution time rather than
defaulting to "allow" — that is what separates this from the service locator banned in
PATTERNS_BACKEND §11.
"""

from collections.abc import Callable
from typing import Final, Protocol

from apps.workflow.domain.errors import GuardNotRegistered
from apps.workflow.domain.value_objects import GuardOutcome, TransitionSubject


class Guard(Protocol):
    """A pure decision about one proposed move.

    Total and side-effect free: it may not query, may not read the clock (`subject.now` is the
    clock) and must return an outcome for every subject rather than raising. Raising would make a
    guard indistinguishable from a bug at the call site.
    """

    def __call__(self, subject: TransitionSubject) -> GuardOutcome:
        """Decide whether `subject` may move, and say why not."""
        ...


_GUARDS: Final[dict[str, Guard]] = {}


def register_guard(code: str) -> Callable[[Guard], Guard]:
    """Register a guard under the code an operator types into `WorkflowTransition.guard`.

    The code is part of the operational contract once a transition row references it: renaming it
    silently disables the check on every edge that uses it, so it is treated as frozen.

    Args:
        code: Registry key, matching `WorkflowTransition.guard`.

    Returns:
        The decorator that stores the guard and returns it unchanged.

    Raises:
        ValueError: The code is already registered, which would otherwise shadow a guard that
            existing transition rows still name.
    """

    def decorator(guard: Guard) -> Guard:
        if code in _GUARDS:
            message = f"A guard is already registered under '{code}'."
            raise ValueError(message)
        _GUARDS[code] = guard
        return guard

    return decorator


def resolve_guard(code: str) -> Guard:
    """Look up the guard a transition names.

    Args:
        code: Non-empty `WorkflowTransition.guard` value. The empty string means "no guard" and is
            handled by the caller, which never reaches this function.

    Returns:
        The registered callable.

    Raises:
        GuardNotRegistered: Nothing is registered under that code — an admin typo, or a guard
            module that was never imported. Raising keeps a claimed safety check from evaporating.
    """
    guard = _GUARDS.get(code)
    if guard is None:
        raise GuardNotRegistered(code)
    return guard


def registered_guard_codes() -> tuple[str, ...]:
    """Every registered code, sorted, for the admin's guard picker and for tests."""
    return tuple(sorted(_GUARDS))
