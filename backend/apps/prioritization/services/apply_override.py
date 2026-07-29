"""Use case: a human forces a project's position in the queue, on the record.

The override is stored beside the computed score and never inside it. Writing it into
``PriorityScore.value`` would make the forced number indistinguishable from a computed one, would
destroy the ability to label the row as an override in the UI, and would require a recomputation
to undo something that should be reversible by revoking a row.
"""

from datetime import datetime
from decimal import Decimal

from django.db import transaction
from pydantic import BaseModel, ConfigDict

from apps.portfolio.models import Project

from ..domain.errors import (
    OverrideMechanismAmbiguous,
    OverrideReasonRequired,
    ProjectNotFound,
)
from ..models import PriorityOverride


class ApplyOverrideCommand(BaseModel):
    """The intent to force a position or nudge a score.

    A command model rather than seven parameters, so the API boundary, the admin action and a
    test all construct the same validated shape and a new field cannot be forgotten at one of the
    three call sites.
    """

    model_config = ConfigDict(frozen=True)

    project_code: str
    actor: str
    reason: str
    position: int | None = None
    boost: Decimal | None = None
    expires_at: datetime | None = None


class OverrideResult(BaseModel):
    """The override that is now in force, and what it replaced.

    ``replaced_override_id`` is reported rather than hidden because "who overrode whose override"
    is the question asked when the queue looks wrong.
    """

    model_config = ConfigDict(frozen=True)

    project_code: str
    override_id: int
    replaced_override_id: int | None
    position: int | None
    boost: Decimal | None
    expires_at: datetime | None


@transaction.atomic
def apply_override(*, command: ApplyOverrideCommand, now: datetime) -> OverrideResult:
    """Put a manual override in force, revoking whatever it replaces.

    At most one override is live per project, so an existing one is revoked at ``now`` in the same
    transaction — the partial unique index would otherwise reject the insert, and silently
    updating the old row would erase who forced what and when.

    The computed ``PriorityScore`` is not read and not written here. That is the invariant this
    service exists to protect.

    Args:
        command: The validated intent, carrying the mandatory reason.
        now: The instant used as the revocation time of the replaced override.

    Returns:
        The identifiers of the new override and of the one it replaced.

    Raises:
        OverrideReasonRequired: The reason is empty or whitespace only.
        OverrideMechanismAmbiguous: Neither or both of ``position`` and ``boost`` were given.
        ProjectNotFound: The command names no existing project.
    """
    if not command.reason.strip():
        raise OverrideReasonRequired(command.project_code)
    if (command.position is None) == (command.boost is None):
        raise OverrideMechanismAmbiguous(command.project_code)

    project_id = _resolve_project_id(command.project_code)

    replaced = PriorityOverride.objects.for_project(project_id).live().first()
    if replaced is not None:
        replaced.revoked_at = now
        replaced.save(update_fields=["revoked_at"])

    override = PriorityOverride.objects.create(
        project_id=project_id,
        position=command.position,
        boost=command.boost,
        reason=command.reason.strip(),
        actor=command.actor,
        expires_at=command.expires_at,
    )

    return OverrideResult(
        project_code=command.project_code,
        override_id=override.pk,
        replaced_override_id=replaced.pk if replaced is not None else None,
        position=override.position,
        boost=override.boost,
        expires_at=override.expires_at,
    )


def _resolve_project_id(project_code: str) -> int:
    """Numeric id of the project, through the portfolio context's own named query."""
    project = Project.objects.by_code(project_code).first()
    if project is None:
        raise ProjectNotFound(project_code)
    return int(project.pk)
