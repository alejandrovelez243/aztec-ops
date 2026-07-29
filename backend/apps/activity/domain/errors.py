"""Typed errors raised by the activity context.

Every error descends from :class:`ActivityError` so the API's central exception handler maps
the family once instead of per route. The append-only pair is deliberately an error and not a
silently ignored write: an audit trail that fails quietly is worse than one that fails loudly.
"""


class ActivityError(Exception):
    """Base class for every failure the activity context can raise."""


class ActivityRecordIsAppendOnly(ActivityError):
    """An existing ActivityRecord was saved again.

    Raised by ``ActivityRecord.save()`` when the instance already exists in the database.
    Correcting the trail means appending a new record, never rewriting the old one.
    """

    def __init__(self, record_id: int) -> None:
        super().__init__(
            f"ActivityRecord {record_id} is append-only and cannot be updated; "
            f"append a new record instead."
        )
        self.record_id = record_id


class ActivityRecordCannotBeDeleted(ActivityError):
    """A deletion of an ActivityRecord was attempted.

    Raised by ``ActivityRecord.delete()`` unconditionally. There is no legitimate caller: a
    trail whose rows can disappear cannot answer "why was this project deprioritized".
    """

    def __init__(self, record_id: int | None) -> None:
        identifier = "unsaved" if record_id is None else str(record_id)
        super().__init__(f"ActivityRecord {identifier} is append-only and cannot be deleted.")
        self.record_id = record_id


class UnknownActivityVerb(ActivityError):
    """The command named a verb outside the structural vocabulary.

    The verb set is closed and versioned by migration; an unknown value means the caller
    invented a fact the timeline cannot render.
    """

    def __init__(self, verb: str) -> None:
        super().__init__(f"'{verb}' is not a known ActivityRecord verb.")
        self.verb = verb


class UnknownActivityOrigin(ActivityError):
    """The command named an origin outside ``MANUAL`` / ``POLICY`` / ``SYSTEM``."""

    def __init__(self, origin: str) -> None:
        super().__init__(f"'{origin}' is not a known ActivityRecord origin.")
        self.origin = origin


class UnknownEntityType(ActivityError):
    """The command named an entity type outside ``project`` / ``task`` / ``blocker``."""

    def __init__(self, entity_type: str) -> None:
        super().__init__(f"'{entity_type}' is not a known ActivityRecord entity type.")
        self.entity_type = entity_type


class ReasonRequiredForManualActivity(ActivityError):
    """A ``MANUAL`` record carried no reason.

    ``MANUAL`` means a human overrode what the system computed. Without the sentence that
    justified it, the record is indistinguishable from a bug three weeks later, which is the
    same rule ``PriorityOverride.reason`` enforces in the database.
    """

    def __init__(self, verb: str, entity_id: str) -> None:
        super().__init__(
            f"A MANUAL {verb} record for {entity_id} must carry a reason explaining the override."
        )
        self.verb = verb
        self.entity_id = entity_id
