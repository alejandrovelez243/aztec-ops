"""How a before/after field pair is rendered into an event payload (`docs/EVENTS.md` §4).

Every ``*.updated`` topic carries the same shape — a ``changes`` mapping of field name to
``{"from", "to"}`` — so the values inside it must be rendered the same way by every producer.
A consumer reading ``changes["priority"]["to"]`` cannot be asked to know that one context sends
a primary key and another a business code.

Pure Python plus Pydantic's ``JsonValue``: no Django, no model import, so the rendering can be
asserted on ``SimpleTestCase`` and reused from any context's ``services/``.
"""

from datetime import date, datetime
from decimal import Decimal

from pydantic import JsonValue


def render_change_value(value: object) -> JsonValue:
    """Render one side of a before/after pair for an ``*.updated`` payload.

    A referenced row is rendered by its business ``code``, never its primary key: a consumer in
    another context must be able to read the change without a foreign key into the emitting one
    (EVENTS.md §1). A ``Decimal`` becomes a float and a ``date``/``datetime`` an ISO-8601 string,
    because the payload is validated inside the producing transaction — a value that cannot
    round-trip through JSON has to abort the edit rather than the later publish, where the
    mutation is already committed.

    ``None`` is preserved as ``null`` rather than flattened to an empty string: "cleared" and
    "set to empty" are different facts, and a consumer that cannot tell them apart recomputes
    the wrong thing.

    Args:
        value: The stored attribute value, of any type a model field can hold.

    Returns:
        A JSON-native value. Anything not otherwise recognised falls back to ``str(value)``,
        which keeps the payload serializable at the cost of precision for a type no topic
        currently carries.
    """
    if value is None or isinstance(value, str | bool | int | float):
        return value
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, date | datetime):
        return value.isoformat()
    code = getattr(value, "code", None)
    return code if isinstance(code, str) else str(value)
