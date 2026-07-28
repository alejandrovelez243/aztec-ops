"""The write port of the audit trail.

Every other bounded context appends through this module. It is deliberately the only way in:
a second writer would be a second definition of what a valid record is, and the trail would
start disagreeing with itself.
"""

from apps.activity.domain.errors import (
    ReasonRequiredForManualActivity,
    UnknownActivityOrigin,
    UnknownActivityVerb,
    UnknownEntityType,
)
from apps.activity.domain.value_objects import ActivityCommand, ActivityEntry
from apps.activity.models import ActivityRecord


def write_activity(command: ActivityCommand) -> ActivityEntry:
    """Append one fact to the audit trail and return it as a value.

    The caller is already inside ``transaction.atomic()``. That is not a convenience: the
    record must commit with the aggregate mutation and the ``OutboxEvent`` it describes, or
    the trail claims a change that was rolled back. This function therefore opens no
    transaction of its own and never swallows an error — a failure here must take the caller's
    use case down with it.

    ``correlation_id`` is what turns several records into one decision. Deprioritizing A in
    order to prioritize B is two ``PRIORITY_CHANGED`` records written under the same
    ``correlation_id``; the detail view reconstructs the whole movement from either half
    through ``repositories.decision_trail``. A caller that generates a fresh id per record
    destroys that link, so the id is threaded down from the API boundary, or forwarded from
    the incoming envelope inside a consumer.

    ``PRIORITY_CHANGED`` carries an ``origin`` that says whether the number is arguable:
    ``MANUAL`` when a human forced the position — the reason is then mandatory and is checked
    here — and ``POLICY`` when the engine recomputed, in which case ``metadata`` names the
    signal that moved and by how much, e.g. ``{"signal": "blockage", "delta": 8.4}``.

    Args:
        command: The fact to append, already shape-validated by Pydantic.

    Returns:
        The persisted entry, carrying the identity the database assigned.

    Raises:
        UnknownEntityType: ``entity_type`` is outside project / task / blocker.
        UnknownActivityVerb: ``verb`` is outside the structural verb set.
        UnknownActivityOrigin: ``origin`` is outside MANUAL / POLICY / SYSTEM.
        ReasonRequiredForManualActivity: ``origin`` is MANUAL and ``reason`` is blank.
    """
    if command.entity_type not in ActivityRecord.EntityType.values:
        raise UnknownEntityType(command.entity_type)
    if command.verb not in ActivityRecord.Verb.values:
        raise UnknownActivityVerb(command.verb)
    if command.origin not in ActivityRecord.Origin.values:
        raise UnknownActivityOrigin(command.origin)
    if command.origin == ActivityRecord.Origin.MANUAL and not command.reason.strip():
        raise ReasonRequiredForManualActivity(command.verb, command.entity_id)

    record = ActivityRecord.objects.create(
        entity_type=command.entity_type,
        entity_id=command.entity_id,
        verb=command.verb,
        origin=command.origin,
        actor=command.actor,
        from_value=command.from_value,
        to_value=command.to_value,
        reason=command.reason,
        metadata=command.metadata,
        occurred_at=command.occurred_at,
        correlation_id=command.correlation_id,
    )
    return ActivityEntry(id=record.pk, **command.model_dump())
