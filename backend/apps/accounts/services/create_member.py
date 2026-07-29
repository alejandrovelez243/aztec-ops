"""Use case: register a person on the roster."""

from datetime import datetime
from uuid import UUID

from django.db import transaction

from apps.accounts.domain.commands import CreateMemberCommand
from apps.accounts.domain.errors import DuplicateMemberCode
from apps.accounts.domain.events import MemberCreatedPayload
from apps.accounts.domain.views import MemberView
from apps.accounts.models import User
from apps.accounts.services._references import resolve_role
from apps.accounts.services.password import hash_password_onto
from apps.activity.domain.value_objects import ActivityCommand
from apps.activity.services import write_activity
from apps.events.domain.envelope import ENTITY_MEMBER, TOPIC_MEMBER_CREATED
from apps.events.services import enqueue_event


@transaction.atomic
def create_member(
    command: CreateMemberCommand,
    *,
    actor: str,
    correlation_id: UUID,
    now: datetime,
) -> MemberView:
    """Register a person, with or without a credential.

    ``username`` is set to ``code`` and never diverges from it. Django's authentication machinery
    addresses an account by ``username`` while every payload in this system addresses a person by
    ``code``; letting the two drift would mean an operator signs in as one identifier and appears
    in the audit trail as another.

    A person created without a password gets ``set_unusable_password()`` — a real assignee who
    cannot sign in yet, which is exactly the state the seeded roster is in. It is not a hole: an
    unusable hash matches no input at all, so the account is unreachable until somebody sets a
    credential through :func:`~apps.accounts.services.set_member_password.set_member_password`.

    The row, the ``ActivityRecord`` and the ``OutboxEvent`` commit together or not at all. Nothing
    is published to Redis here; the drain delivers the outbox row after commit, which is what makes
    the event as atomic as the person.

    Args:
        command: The person to register, addressed by business codes.
        actor: ``accounts.User.code`` of whoever is registering them.
        correlation_id: Threaded from the API boundary so related changes reconstruct as one
            movement.
        now: Domain time. A parameter, never ``timezone.now()`` inside the body, so a replay is
            deterministic and the service is testable.

    Returns:
        The registered person as a frozen projection.

    Raises:
        DuplicateMemberCode: The code already belongs to somebody.
        RoleNotFound: ``role_code`` matches no role.
        PasswordRejected: A password was supplied and did not survive Django's validators.
    """
    if User.objects.by_code(command.code).exists():
        raise DuplicateMemberCode(command.code)

    role = resolve_role(command.role_code)
    member = User(
        code=command.code,
        username=command.code,
        alias=command.label,
        role=role,
        weekly_capacity_points=command.weekly_capacity_points,
    )
    hash_password_onto(member, command.password)
    member.save()

    write_activity(
        ActivityCommand(
            entity_type=ENTITY_MEMBER,
            entity_id=member.code,
            verb="CREATED",
            actor=actor,
            to_value=member.alias,
            metadata={
                "role": role.code if role is not None else "",
                "weekly_capacity_points": member.weekly_capacity_points,
                "has_password": member.has_usable_password(),
            },
            occurred_at=now,
            correlation_id=correlation_id,
        )
    )
    enqueue_event(
        topic=TOPIC_MEMBER_CREATED,
        entity_type=ENTITY_MEMBER,
        entity_id=member.code,
        payload=MemberCreatedPayload(
            label=member.alias,
            role=role.code if role is not None else None,
            weekly_capacity_points=member.weekly_capacity_points,
            is_active=member.is_active,
        ).model_dump(mode="json"),
        actor=actor,
        correlation_id=correlation_id,
        occurred_at=now,
    )

    return member.to_member_view()
