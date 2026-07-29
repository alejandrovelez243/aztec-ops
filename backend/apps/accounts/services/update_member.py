"""Use case: edit a person's roster attributes, including retiring and restoring them."""

from datetime import datetime
from uuid import UUID

from django.db import transaction

from apps.accounts.domain.commands import UpdateMemberCommand
from apps.accounts.domain.errors import CannotDeactivateSelf, MemberNotFound
from apps.accounts.domain.events import (
    FIELD_CAPACITY,
    FIELD_LABEL,
    FIELD_ROLE,
    MemberActivationChangedPayload,
    MemberUpdatedPayload,
)
from apps.accounts.domain.views import MemberView
from apps.accounts.models import User
from apps.accounts.services._references import resolve_role
from apps.activity.domain.value_objects import ActivityCommand
from apps.activity.services import write_activity
from apps.events.domain.envelope import (
    ENTITY_MEMBER,
    TOPIC_MEMBER_ACTIVATION_CHANGED,
    TOPIC_MEMBER_UPDATED,
)
from apps.events.services import enqueue_event

#: Wire field name to the activity verb that records its movement. A table rather than a chain of
#: ``if``s so adding an editable attribute is a row here plus a verb, never a new branch.
_VERB_BY_FIELD: dict[str, str] = {
    FIELD_LABEL: "RENAMED",
    FIELD_ROLE: "ROLE_CHANGED",
    FIELD_CAPACITY: "CAPACITY_CHANGED",
}

#: The command attribute that carries the wire's ``role``. The two names differ — the payload sends
#: a role, the command names it as the *code* it resolves — so ``model_fields_set``, which reports
#: command attributes, has to be asked about this one under its own name. Reading it as ``"role"``
#: silently answers "the caller did not send a role" for every request that did.
_ROLE_COMMAND_FIELD = "role_code"


@transaction.atomic
def update_member(
    command: UpdateMemberCommand,
    *,
    actor: str,
    correlation_id: UUID,
    now: datetime,
) -> MemberView:
    """Apply the fields the caller actually sent, and record only what actually moved.

    Absent and ``None`` are different requests: an absent ``role_code`` leaves the role alone, an
    explicit ``None`` unclassifies the person. The distinction is read from the command's
    ``model_fields_set``, which the router carried down from the payload.

    **A no-op writes nothing.** Sending a person their current values produces no activity record
    and no event, because a trail that records non-changes cannot be read for changes and an event
    that announces nothing makes every subscriber refetch for nothing.

    Retiring and restoring emit ``member.activation_changed``, separately from the attribute edits'
    ``member.updated``. A single ``PATCH`` doing both therefore emits both — two facts happened,
    and collapsing them would force every subscriber to parse one payload for two questions.

    The row is locked for the duration, so two operators editing the same person serialize instead
    of the second silently overwriting the first's capacity with a value computed before it.

    Args:
        command: Who to edit and which fields to touch.
        actor: ``accounts.User.code`` of whoever is editing.
        correlation_id: Threaded from the API boundary; every record and event this call produces
            shares it.
        now: Domain time, supplied by the caller.

    Returns:
        The person as they now stand.

    Raises:
        MemberNotFound: ``command.code`` matches nobody.
        RoleNotFound: ``role_code`` was sent and matches no role.
        CannotDeactivateSelf: The caller tried to retire their own account.
    """
    # Locked without ``select_related``: ``role`` is nullable, so joining it would put the lock on
    # the nullable side of an outer join, which PostgreSQL refuses outright.
    member = User.objects.select_for_update().by_code(command.code).first()
    if member is None:
        raise MemberNotFound(command.code)

    sent = command.model_fields_set
    if "is_active" in sent and command.is_active is False and command.code == actor:
        raise CannotDeactivateSelf(command.code)

    changed = _apply_attributes(member, command, sent=sent)
    activation_changed = _apply_activation(member, command, sent=sent)
    if not changed and not activation_changed:
        return member.to_member_view()

    member.save()

    if changed:
        _record_attribute_changes(
            member, changed, actor=actor, correlation_id=correlation_id, now=now
        )
    if activation_changed:
        _record_activation_change(member, actor=actor, correlation_id=correlation_id, now=now)

    return member.to_member_view()


def _apply_attributes(
    member: User, command: UpdateMemberCommand, *, sent: set[str]
) -> dict[str, tuple[str, str]]:
    """Mutate the instance in memory and report which wire fields moved, and from what.

    Returns the before/after pair per field rather than a bare set of names, because the activity
    record's whole value is ``from_value`` — "capacity changed" without the previous number cannot
    answer why somebody stopped being overloaded.

    Args:
        member: The locked row, mutated in place and not saved here.
        command: The requested edit.
        sent: The fields the caller actually included.

    Returns:
        Wire field name to ``(before, after)``, rendered as the text the trail stores. Empty when
        the request asked for values the person already had.

    Raises:
        RoleNotFound: ``role_code`` was sent and matches no role.
    """
    changed: dict[str, tuple[str, str]] = {}

    if FIELD_LABEL in sent and command.label is not None and command.label != member.alias:
        changed[FIELD_LABEL] = (member.alias, command.label)
        member.alias = command.label

    if _ROLE_COMMAND_FIELD in sent:
        role = resolve_role(command.role_code)
        before = member.role.code if member.role is not None else ""
        after = role.code if role is not None else ""
        if before != after:
            changed[FIELD_ROLE] = (before, after)
            member.role = role

    if FIELD_CAPACITY in sent and command.weekly_capacity_points is not None:
        before_points = member.weekly_capacity_points
        if before_points != command.weekly_capacity_points:
            changed[FIELD_CAPACITY] = (
                str(before_points),
                str(command.weekly_capacity_points),
            )
            member.weekly_capacity_points = command.weekly_capacity_points

    return changed


def _apply_activation(member: User, command: UpdateMemberCommand, *, sent: set[str]) -> bool:
    """Mutate ``is_active`` in memory and report whether it moved.

    Separate from the attribute pass because retiring somebody is a different fact with a different
    topic, and because it is the only edit with a guard in front of it.

    Args:
        member: The locked row, mutated in place and not saved here.
        command: The requested edit.
        sent: The fields the caller actually included.

    Returns:
        Whether the person's participation in the operation changed.
    """
    if "is_active" not in sent or command.is_active is None:
        return False
    if command.is_active == member.is_active:
        return False
    member.is_active = command.is_active
    return True


def _record_attribute_changes(
    member: User,
    changed: dict[str, tuple[str, str]],
    *,
    actor: str,
    correlation_id: UUID,
    now: datetime,
) -> None:
    """Write one activity record per moved attribute, then one event for all of them.

    One record per field and one event for the set, deliberately. The trail is read per fact — "who
    raised this capacity" is a filter on ``CAPACITY_CHANGED`` — while a subscriber wants the
    person's new state once, not three deliveries of it. They share a ``correlation_id``, so the
    trail still reconstructs the edit as a single decision.
    """
    for field_name, (before, after) in changed.items():
        write_activity(
            ActivityCommand(
                entity_type=ENTITY_MEMBER,
                entity_id=member.code,
                verb=_VERB_BY_FIELD[field_name],
                actor=actor,
                from_value=before,
                to_value=after,
                metadata={"field": field_name},
                occurred_at=now,
                correlation_id=correlation_id,
            )
        )
    enqueue_event(
        topic=TOPIC_MEMBER_UPDATED,
        entity_type=ENTITY_MEMBER,
        entity_id=member.code,
        payload=MemberUpdatedPayload.of(
            label=member.alias,
            role=member.role.code if member.role is not None else None,
            weekly_capacity_points=member.weekly_capacity_points,
            changed=tuple(changed),
        ).model_dump(mode="json"),
        actor=actor,
        correlation_id=correlation_id,
        occurred_at=now,
    )


def _record_activation_change(
    member: User, *, actor: str, correlation_id: UUID, now: datetime
) -> None:
    """Write the retire-or-restore fact and announce it.

    The verb names the direction while the payload carries the boolean: a person reading the trail
    wants to see "DEACTIVATED", and a subscriber wants a field it can branch on without a lookup
    table of verbs it does not otherwise know.
    """
    write_activity(
        ActivityCommand(
            entity_type=ENTITY_MEMBER,
            entity_id=member.code,
            verb="REACTIVATED" if member.is_active else "DEACTIVATED",
            actor=actor,
            from_value=str(not member.is_active).lower(),
            to_value=str(member.is_active).lower(),
            metadata={"field": "is_active"},
            occurred_at=now,
            correlation_id=correlation_id,
        )
    )
    enqueue_event(
        topic=TOPIC_MEMBER_ACTIVATION_CHANGED,
        entity_type=ENTITY_MEMBER,
        entity_id=member.code,
        payload=MemberActivationChangedPayload(
            label=member.alias, is_active=member.is_active
        ).model_dump(mode="json"),
        actor=actor,
        correlation_id=correlation_id,
        occurred_at=now,
    )
