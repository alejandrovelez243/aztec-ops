"""Use cases of the ``work`` context, one public function per module.

Every one of them has the same anatomy, in this order: validate with typed domain errors,
mutate the aggregate, append an ``ActivityRecord``, write an ``OutboxEvent`` — all inside a
single ``transaction.atomic()``, so a caller either sees all four effects or none of them.

None of these modules imports Redis. Publishing an event from a service would not be rolled
back with the transaction that produced it, so a later ``IntegrityError`` would leave a
published fact describing a change that never happened. The row in the outbox is as atomic
as the change; delivery is the relay's problem and the relay retries.

**On ``origin``.** ``MANUAL`` is not "a human did it", it is "a human forced it and owes an
explanation" — which is why ``write_activity`` refuses a ``MANUAL`` record with a blank
reason. Raising and resolving a blocker always carry one, so they are ``MANUAL``; a
transition is ``MANUAL`` when the operator gave a reason and ``SYSTEM`` when the declared
edge did not ask for one. Ordinary edits are ``SYSTEM``: they are recorded facts, not
contested decisions. ``POLICY`` belongs to the engine and is never written here.
"""

from typing import Final

#: A human forcing a change and justifying it; ``reason`` is mandatory alongside it.
ORIGIN_MANUAL: Final = "MANUAL"

#: A recorded change with nothing to argue about.
ORIGIN_SYSTEM: Final = "SYSTEM"
