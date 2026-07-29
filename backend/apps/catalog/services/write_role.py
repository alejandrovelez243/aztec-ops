"""Use cases: add a role to the vocabulary, and rename or retire one.

The two live in one module because they are two halves of one decision — what jobs this operation
recognises — and because they share the rule that makes them safe to expose at all: **every change
here writes an ``ActivityRecord``**. A taxonomy row is what the rest of the product compares
against, so renaming or retiring one changes what a filter, a picker and a person's row all mean.
The trail is the difference between "editable from the product" and "editable by nobody in
particular".

No event is emitted. A role is picker vocabulary: nothing recomputes when it changes, no read
model denormalizes it, and the surfaces that render one refetch the catalog. A topic here would be
a broadcast with no subscriber that could act on it.
"""

from datetime import datetime
from uuid import UUID

from django.db import transaction

from apps.activity.domain.value_objects import ActivityCommand
from apps.activity.services import write_activity
from apps.catalog.domain.commands import CreateRoleCommand, UpdateRoleCommand
from apps.catalog.domain.errors import DuplicateTaxonomyCode, TaxonomyRowNotFound
from apps.catalog.models import Role
from apps.shared.refs import TaxonomyRef

#: ``entity_type`` of the trail entries these use cases write, and the human name the typed errors
#: put in their message. One constant so the two cannot disagree.
ROLE_ENTITY = "role"


@transaction.atomic
def create_role(
    command: CreateRoleCommand,
    *,
    actor: str,
    correlation_id: UUID,
    now: datetime,
) -> TaxonomyRef:
    """Add a role, active and last in the operator's ordering.

    ``order`` is not a parameter. A new role goes to the end of the picker, which is where a
    reader expects to find the thing that was just added; rearranging the vocabulary is a
    deliberate act done in the admin, not a number typed into a form that was really about
    registering a person.

    Args:
        command: The role to add.
        actor: ``accounts.User.code`` of whoever is adding it.
        correlation_id: Threaded from the API boundary.
        now: Domain time, supplied by the caller.

    Returns:
        The role as the reference every picker renders.

    Raises:
        DuplicateTaxonomyCode: The code is taken — including by a retired role, which is restored
            rather than duplicated.
    """
    if Role.objects.find_by_code(command.code) is not None:
        raise DuplicateTaxonomyCode(ROLE_ENTITY, command.code)

    last = Role.objects.order_by("-order").first()
    role = Role.objects.create(
        code=command.code,
        label=command.label,
        order=(last.order + 1) if last is not None else 0,
    )

    write_activity(
        ActivityCommand(
            entity_type=ROLE_ENTITY,
            entity_id=role.code,
            verb="CREATED",
            actor=actor,
            to_value=role.label,
            occurred_at=now,
            correlation_id=correlation_id,
        )
    )
    return role.to_ref()


@transaction.atomic
def update_role(
    command: UpdateRoleCommand,
    *,
    actor: str,
    correlation_id: UUID,
    now: datetime,
) -> TaxonomyRef:
    """Rename a role, retire it, or restore it, recording only what actually moved.

    Absent means untouched. Sending a role its current label and its current state is a successful
    no-op that writes nothing: a trail that records non-changes cannot be read for changes.

    Args:
        command: Which role, and which fields to touch.
        actor: ``accounts.User.code`` of whoever is editing.
        correlation_id: Threaded from the API boundary; every record this call writes shares it.
        now: Domain time, supplied by the caller.

    Returns:
        The role as it now stands.

    Raises:
        TaxonomyRowNotFound: ``command.code`` matches no role.
    """
    role = Role.objects.select_for_update().filter(code=command.code).first()
    if role is None:
        raise TaxonomyRowNotFound(ROLE_ENTITY, command.code)

    sent = command.model_fields_set
    records: list[ActivityCommand] = []

    if "label" in sent and command.label is not None and command.label != role.label:
        records.append(
            _record(
                role.code,
                verb="RENAMED",
                actor=actor,
                before=role.label,
                after=command.label,
                now=now,
                correlation_id=correlation_id,
            )
        )
        role.label = command.label

    if (
        "is_active" in sent
        and command.is_active is not None
        and command.is_active != role.is_active
    ):
        role.is_active = command.is_active
        records.append(
            _record(
                role.code,
                verb="REACTIVATED" if role.is_active else "DEACTIVATED",
                actor=actor,
                before=str(not role.is_active).lower(),
                after=str(role.is_active).lower(),
                now=now,
                correlation_id=correlation_id,
            )
        )

    if not records:
        return role.to_ref()

    role.save(update_fields=["label", "is_active"])
    for record in records:
        write_activity(record)
    return role.to_ref()


def _record(  # noqa: PLR0913 - one call site; the trail entry has this many parts.
    code: str,
    *,
    verb: str,
    actor: str,
    before: str,
    after: str,
    now: datetime,
    correlation_id: UUID,
) -> ActivityCommand:
    """Build one trail entry about a role, so the two branches above cannot describe it twice."""
    return ActivityCommand(
        entity_type=ROLE_ENTITY,
        entity_id=code,
        verb=verb,
        actor=actor,
        from_value=before,
        to_value=after,
        occurred_at=now,
        correlation_id=correlation_id,
    )
