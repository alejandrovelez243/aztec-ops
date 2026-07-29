"""Payload schemas of the three roster topics (EVENTS.md §4).

Pure: no Django, no model, no envelope. The envelope is assembled by ``apps.events.services``; what
lives here is only the body, so a payload can be constructed and asserted on ``SimpleTestCase``.

**No credential ever appears in these payloads**, and there is deliberately no
``member.password_reset`` topic. An event is a fact broadcast to every subscriber and replayed from
a durable row months later; "somebody's password was replaced" is of interest to exactly nobody on
the bus, and putting the fact on a channel the browser reads would leak the timing of credential
changes to every open tab for no consumer's benefit. The reset is recorded in the audit trail,
where the question is actually asked.

``member.updated`` names which fields moved in ``changed``. A consumer deciding whether to act —
a capacity change alters what "overloaded" means, an email change does not — would otherwise have
to diff the payload against a copy it does not have.
"""

from typing import Self

from pydantic import BaseModel, ConfigDict, Field

#: ``changed`` entries of ``member.updated``. Wire names, matching the ``PATCH`` body's fields
#: rather than the model's columns, so a subscriber compares against what it can also send.
FIELD_LABEL = "label"
FIELD_ROLE = "role"
FIELD_CAPACITY = "weekly_capacity_points"
FIELD_EMAIL = "email"


class MemberCreatedPayload(BaseModel):
    """Body of ``member.created``.

    Carries the whole person rather than only the code, so a subscriber renders the new row
    without a follow-up read. Load is absent on purpose: it is computed from task rows at read
    time and a freshly registered person has none, so a number here could only be a zero that
    goes stale the moment they are assigned anything.
    """

    model_config = ConfigDict(frozen=True)

    label: str
    role: str | None = None
    weekly_capacity_points: int = Field(gt=0)
    is_active: bool = True


class MemberUpdatedPayload(BaseModel):
    """Body of ``member.updated``, carrying the new values and naming what moved.

    ``changed`` is never empty: a service that found nothing to change emits nothing at all, so an
    event with an empty ``changed`` would be a claim that something happened when it did not.

    Emitted only for the attribute edits. Retiring or restoring somebody is
    ``member.activation_changed``, because the question a subscriber asks about it — "may this
    person still take work?" — has a different answer shape from "their name is now spelled
    differently".
    """

    model_config = ConfigDict(frozen=True)

    label: str
    role: str | None = None
    weekly_capacity_points: int = Field(gt=0)
    changed: tuple[str, ...] = Field(min_length=1)

    @classmethod
    def of(
        cls,
        *,
        label: str,
        role: str | None,
        weekly_capacity_points: int,
        changed: tuple[str, ...],
    ) -> Self:
        """Build the payload from the values the person now carries.

        Args:
            label: The display name after the edit.
            role: ``catalog.Role.code`` after the edit, or ``None`` if unclassified.
            weekly_capacity_points: The capacity after the edit.
            changed: The wire field names that actually moved.

        Returns:
            The payload, ready to dump into the envelope.
        """
        return cls(
            label=label,
            role=role,
            weekly_capacity_points=weekly_capacity_points,
            changed=changed,
        )


class MemberActivationChangedPayload(BaseModel):
    """Body of ``member.activation_changed``.

    One topic for both directions, carrying ``is_active``, rather than a retire topic and a restore
    topic. A subscriber's question is a boolean and answering it with a topic name would force
    every subscription to list both and treat them identically — which is the shape of the bug
    where somebody later adds only one of the two.

    The person's existing work is untouched: ``is_active`` retires them from *new* assignment and
    nothing reassigns what they already hold, so a consumer must not read this as "their tasks are
    now unowned".
    """

    model_config = ConfigDict(frozen=True)

    label: str
    is_active: bool
