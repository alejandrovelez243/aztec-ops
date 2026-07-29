"""Immutable values crossing the boundary of the activity context.

``ActivityCommand`` is what a caller in another context hands to the write port;
``ActivityEntry`` is what comes back and what the timeline reads render. Both are frozen: a
record of what was true at an instant that can be mutated afterwards is not a record.

The string lengths below mirror ``activity_activityrecord`` column by column (DATA_MODEL §5),
so an over-long value fails at construction with the field name rather than as a database
error thrown halfway through the caller's transaction.
"""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, JsonValue

#: Default page size of a timeline read, matching the LIMIT in DATA_MODEL §10.2.
TIMELINE_PAGE_SIZE: int = 50


class ActivityCommand(BaseModel):
    """The intent to append one fact to the audit trail.

    Constructing one is free of side effects; only ``write_activity`` persists it. The command
    carries point-in-time copies of the values that changed — never identifiers to be resolved
    later — because ``from_value`` and ``to_value`` must stay readable after the aggregate has
    moved on (DATA_MODEL §11).

    Attributes:
        entity_type: ``project`` | ``task`` | ``blocker``.
        entity_id: The **business code** (``PRJ-01``), matching ``entity.id`` in the event
            envelope. Never a numeric primary key: a consumer in another context must be able
            to join on it without a foreign key into this one.
        verb: One of the structural verbs; validated by the service against the model's set.
        origin: ``MANUAL`` when a human forced the change and must justify it, ``POLICY`` when
            the engine recomputed and ``metadata`` names the signal that moved, ``SYSTEM``
            otherwise.
        actor: ``accounts.User.code``, or ``system`` when no human caused the change. A string
            and not a foreign key on purpose: the prioritization engine and the stream
            consumers write records as ``system``, and a foreign key would force a fake
            person row to exist so they could.
        from_value: Previous value as text. Empty when the fact has no "before" (``CREATED``).
        to_value: New value as text.
        reason: Mandatory when ``origin`` is ``MANUAL``, and whenever the transition or the
            override that produced the record required one.
        metadata: Verb-specific extras, e.g. ``{"blocker_id": 12, "kind": "ACCESS"}`` or
            ``{"signal": "blockage", "delta": 8.4}``.
        occurred_at: Domain time — when the fact happened, not when the row was written. Passed
            in by the caller so a replay does not shift the trail.
        correlation_id: Chains every record produced by one decision. "Deprioritize A in order
            to prioritize B" is two records sharing this value, which is the only way the pair
            can be reconstructed as a single movement.
    """

    model_config = ConfigDict(frozen=True)

    entity_type: str = Field(max_length=16)
    entity_id: str = Field(max_length=32)
    verb: str = Field(max_length=24)
    origin: str = Field(default="SYSTEM", max_length=8)
    actor: str = Field(max_length=32)
    from_value: str = Field(default="", max_length=255)
    to_value: str = Field(default="", max_length=255)
    reason: str = Field(default="", max_length=500)
    metadata: dict[str, JsonValue] = Field(default_factory=dict)
    occurred_at: datetime
    correlation_id: UUID


class ActivityEntry(ActivityCommand):
    """One persisted fact of the audit trail.

    It extends the command with the identity the database assigned, so a caller that wrote a
    record and a reader that queried one hold the same type. Immutable for the same reason the
    table has no ``updated_at``.

    Attributes:
        id: Primary key of the ``activity_activityrecord`` row.
    """

    id: int
