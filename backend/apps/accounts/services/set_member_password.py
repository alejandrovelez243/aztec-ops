"""Use case: replace a person's password on their behalf."""

from datetime import datetime
from uuid import UUID

from django.db import transaction

from apps.accounts.domain.commands import SetMemberPasswordCommand
from apps.accounts.domain.errors import MemberNotFound
from apps.accounts.domain.views import MemberView
from apps.accounts.models import User
from apps.accounts.services.password import hash_password_onto
from apps.activity.domain.value_objects import ActivityCommand
from apps.activity.services import write_activity
from apps.events.domain.envelope import ENTITY_MEMBER


@transaction.atomic
def set_member_password(
    command: SetMemberPasswordCommand,
    *,
    actor: str,
    correlation_id: UUID,
    now: datetime,
) -> MemberView:
    """Set a new password, whether or not the person had one.

    This is an administrative reset, not a change-my-own-password flow: there is no current
    password to verify, because the caller is not the person. Authorization for that lives on the
    route, not here — a service that inspected the caller's privileges would be a second place the
    rule is written, and the API is where the rule is declared and documented.

    It is also the recovery path for a seeded account, which starts with an unusable hash: setting
    a password is what turns a real assignee who cannot sign in into one who can.

    **No event is emitted, on purpose.** ``member.password_reset`` would broadcast the timing of
    credential changes to every open browser tab and sit in a durable outbox row for replay, in
    exchange for a fact no subscriber renders. The audit trail records it — actor, subject and
    instant, never the value — which is where the question is actually asked.

    Args:
        command: Whose credential to replace, and with what.
        actor: ``accounts.User.code`` of whoever is performing the reset.
        correlation_id: Threaded from the API boundary.
        now: Domain time, supplied by the caller.

    Returns:
        The person, whose ``has_password`` is now true.

    Raises:
        MemberNotFound: ``command.code`` matches nobody.
        PasswordRejected: The password did not survive Django's configured validators.
    """
    member = User.objects.select_for_update().by_code(command.code).first()
    if member is None:
        raise MemberNotFound(command.code)

    hash_password_onto(member, command.password)
    member.save(update_fields=["password"])

    write_activity(
        ActivityCommand(
            entity_type=ENTITY_MEMBER,
            entity_id=member.code,
            verb="PASSWORD_RESET",
            actor=actor,
            # No ``from_value`` and no ``to_value``: the trail states that the credential was
            # replaced and by whom, and a record of a password is not a record anybody may keep.
            metadata={"self_service": actor == member.code},
            occurred_at=now,
            correlation_id=correlation_id,
        )
    )

    return member.to_member_view()
