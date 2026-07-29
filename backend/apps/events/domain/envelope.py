"""The event envelope and the topic catalog, exactly as `docs/EVENTS.md` §1 and §4 define them.

This module is the published contract of the bus. Every producer builds an envelope here, the
relay reassembles one here, and every consumer receives one. It is pure Python plus Pydantic: no
Django, no model, no Redis, so an envelope can be constructed and asserted on ``SimpleTestCase``.

Two things are deliberately *not* in this module. Topic membership is validated at enqueue time
(``apps.events.services``), not inside :class:`EventEnvelope`, because an envelope must remain
decodable after a topic is retired — a dead letter replayed months later still has to parse.
And the payload is an opaque JSON mapping: each context declares its own payload model in its own
``domain/events.py`` and dumps it here, so adding a topic never edits this file's schema.
"""

from datetime import UTC, datetime
from typing import Final, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_serializer, field_validator

# --- Topic catalog (EVENTS.md §4) ---------------------------------------------------------
# Topic names are immutable once released: they are matched by consumer subscriptions and by the
# frontend's EventSource listeners. Renaming one means emitting both for a release, not an edit.

TOPIC_PROJECT_CREATED: Final[str] = "project.created"
TOPIC_PROJECT_UPDATED: Final[str] = "project.updated"
TOPIC_PROJECT_STATE_CHANGED: Final[str] = "project.state_changed"
TOPIC_PROJECT_PRIORITY_RECALCULATED: Final[str] = "project.priority.recalculated"
TOPIC_TASK_CREATED: Final[str] = "task.created"
TOPIC_TASK_UPDATED: Final[str] = "task.updated"
TOPIC_TASK_STATE_CHANGED: Final[str] = "task.state_changed"
TOPIC_BLOCKER_RAISED: Final[str] = "blocker.raised"
TOPIC_BLOCKER_RESOLVED: Final[str] = "blocker.resolved"
TOPIC_NOTE_ADDED: Final[str] = "note.added"
TOPIC_CLOCK_TICKED: Final[str] = "clock.ticked"
TOPIC_MEMBER_CREATED: Final[str] = "member.created"
TOPIC_MEMBER_UPDATED: Final[str] = "member.updated"
# Deactivation and reactivation share one topic carrying ``is_active``, rather than being two.
# A consumer's question is "may this person still take work?", and the answer is a field; two
# topics would make every subscriber list both and treat them identically, which is the shape of
# a bug the first time somebody adds only one of them to a subscription.
TOPIC_MEMBER_ACTIVATION_CHANGED: Final[str] = "member.activation_changed"

#: Every registered topic. A topic absent from here does not exist (EVENTS.md §7.1).
ALL_TOPICS: Final[frozenset[str]] = frozenset(
    {
        TOPIC_PROJECT_CREATED,
        TOPIC_PROJECT_UPDATED,
        TOPIC_PROJECT_STATE_CHANGED,
        TOPIC_PROJECT_PRIORITY_RECALCULATED,
        TOPIC_TASK_CREATED,
        TOPIC_TASK_UPDATED,
        TOPIC_TASK_STATE_CHANGED,
        TOPIC_BLOCKER_RAISED,
        TOPIC_BLOCKER_RESOLVED,
        TOPIC_NOTE_ADDED,
        TOPIC_CLOCK_TICKED,
        TOPIC_MEMBER_CREATED,
        TOPIC_MEMBER_UPDATED,
        TOPIC_MEMBER_ACTIVATION_CHANGED,
    }
)

#: The ``sse-fanout`` allowlist (EVENTS.md §5). Defaults to off: a topic reaches the browser only
#: by being named here *and* in ``frontend/src/lib/stream/topics.ts``. ``clock.ticked`` is the
#: standing counter-example — it triggers recomputation and renders nothing, so forwarding it
#: would be noise the client discards.
SSE_ALLOWLIST_TOPICS: Final[frozenset[str]] = ALL_TOPICS - {TOPIC_CLOCK_TICKED}

# --- Entity kinds (EVENTS.md §1, ``entity.type``) -----------------------------------------

ENTITY_PROJECT: Final[str] = "project"
ENTITY_TASK: Final[str] = "task"
ENTITY_BLOCKER: Final[str] = "blocker"
ENTITY_NOTE: Final[str] = "note"
ENTITY_CLOCK: Final[str] = "clock"
#: A person on the roster, addressed by ``accounts.User.code`` — never by primary key, so a
#: consumer acts on the same identifier every other payload already names an owner or assignee by.
ENTITY_MEMBER: Final[str] = "member"

#: ``actor`` when the prioritization or risk engine, the relay or the ticker caused the change.
#: Never null and never empty: an unattributed change is a bug (EVENTS.md §1).
SYSTEM_ACTOR: Final[str] = "system"

#: Payload schema version of a topic that has never had a breaking change.
INITIAL_VERSION: Final[int] = 1

#: The single stream-entry field holding the envelope JSON (EVENTS.md §1, wire formats). A Redis
#: stream entry is a flat map; putting the whole envelope under one key keeps the entry's shape
#: independent of the envelope's, so adding a field is never a stream schema change.
ENVELOPE_FIELD: Final[str] = "data"


def is_registered_topic(topic: str) -> bool:
    """Whether the topic appears in the `docs/EVENTS.md` §4 catalog.

    Callers use this to fail at the emitting service rather than at the consumer, where an
    unknown topic looks like a delivery problem instead of a typo.

    Args:
        topic: Dot-separated topic name, e.g. ``"project.state_changed"``.

    Returns:
        True when the topic is registered and therefore has a documented payload schema.
    """
    return topic in ALL_TOPICS


class EntityRef(BaseModel):
    """The aggregate an event is about, addressed by its business code.

    ``id`` is the business code (``PRJ-01``, ``PRJ-01-T02``), never a database primary key. That
    is the rule that keeps contexts decoupled: a consumer in another context can act on the event
    without a foreign key into the emitter's tables (EVENTS.md §1).
    """

    model_config = ConfigDict(frozen=True)

    type: str = Field(min_length=1, max_length=16)
    id: str = Field(min_length=1, max_length=32)


class EventEnvelope(BaseModel):
    """The fixed envelope carried by every topic on ``aztec.events``.

    Immutable by construction: an envelope is a fact that already committed, so a consumer that
    could mutate it in flight would be rewriting history for whichever handler runs next. New
    information goes inside ``payload``, never at this top level — the envelope is not versioned
    per topic, and changing it is a change to every consumer at once (EVENTS.md §2).

    ``id`` is the deduplication key for every consumer group, the ``id:`` of the SSE frame and the
    value a browser replays through ``Last-Event-ID``. It is generated when the ``OutboxEvent``
    row is written, never by the relay, so the value is known before the producing transaction
    commits.

    Raises:
        pydantic.ValidationError: ``occurred_at`` is naive, ``version`` is below 1, or a required
            envelope field is absent. Construction is the only validation point, so an envelope
            that exists is an envelope that is well-formed.
    """

    model_config = ConfigDict(frozen=True)

    id: UUID
    topic: str = Field(min_length=1, max_length=64)
    occurred_at: datetime
    actor: str = Field(min_length=1, max_length=32)
    correlation_id: UUID
    entity: EntityRef
    # JsonValue rather than Any: the payload lands in a JSONB column and on a Redis stream, so a
    # value that cannot round-trip through JSON must fail here, inside the producer's
    # transaction, rather than at XADD time in the relay where the mutation is already committed.
    payload: dict[str, JsonValue] = Field(default_factory=dict)
    version: int = Field(default=INITIAL_VERSION, ge=1)

    @field_validator("occurred_at")
    @classmethod
    def _reject_naive_datetime(cls, value: datetime) -> datetime:
        """Force aware datetimes, normalized to UTC.

        A naive ``occurred_at`` is silently reinterpreted by whatever timezone the reading process
        happens to run in, which turns "discard a stale redelivery" into a coin flip.
        """
        if value.tzinfo is None:
            message = "occurred_at must be timezone-aware; the bus stores UTC instants."
            raise ValueError(message)
        return value.astimezone(UTC)

    @field_serializer("occurred_at")
    def _serialize_occurred_at(self, value: datetime) -> str:
        """Render ISO-8601 UTC with a ``Z`` suffix, the wire format EVENTS.md §1 fixes."""
        return value.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")

    def to_json(self) -> str:
        """Serialize to the exact JSON the stream entry's ``data`` field holds.

        The relay, the dead letter writer and the SSE frame all use this one representation, so
        an envelope read from the browser's devtools is byte-comparable with the outbox row.
        """
        return self.model_dump_json()

    @classmethod
    def from_json(cls, raw: str) -> Self:
        """Rebuild an envelope from a stream entry's ``data`` field.

        Args:
            raw: The JSON string written by :meth:`to_json`.

        Returns:
            The validated envelope.

        Raises:
            pydantic.ValidationError: The JSON is not a well-formed envelope. Callers on the
                consumer boundary translate this into
                :class:`apps.events.domain.errors.EnvelopeDecodeError` so the entry can be dead
                lettered with its entry id attached.
        """
        return cls.model_validate_json(raw)
