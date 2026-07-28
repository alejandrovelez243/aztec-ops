"""Every query this context runs against its own tables.

Each function is one named query with one caller-visible meaning. "Open flag" is defined here
once — ``cleared_at IS NULL`` — instead of being re-derived in a service, a consumer and the
admin until the three disagree.
"""

from datetime import datetime

from .models import PriorityOverride, PriorityPolicy, PriorityScore, RiskFlag


def get_active_policy() -> PriorityPolicy | None:
    """The single policy row marked active.

    Returns:
        The active policy, or ``None`` when the operation has not activated one — the caller
        raises ``ActivePolicyNotFound`` rather than scoring against an implicit default.
    """
    return PriorityPolicy.objects.filter(is_active=True).first()


def get_score(project_id: int) -> PriorityScore | None:
    """The current score row of one project, if it has ever been computed.

    Args:
        project_id: Numeric primary key of the project.

    Returns:
        The score row, or ``None`` before the first recomputation.
    """
    return PriorityScore.objects.filter(project_id=project_id).first()


def scores_due_for_recompute(*, at: datetime, limit: int) -> list[int]:
    """Project ids whose time-dependent signals have crossed a bucket boundary.

    This is the whole cost of a clock tick: one scan of the ``valid_until`` index. A quiet tick
    returns an empty list and nothing is recomputed.

    Args:
        at: The tick instant.
        limit: Maximum ids to return, so one tick cannot enqueue unbounded work.

    Returns:
        Project ids in ``valid_until`` order, oldest boundary first.
    """
    return list(
        PriorityScore.objects.filter(valid_until__isnull=False, valid_until__lte=at)
        .order_by("valid_until")
        .values_list("project_id", flat=True)[:limit]
    )


def open_flags(project_id: int) -> list[RiskFlag]:
    """The risk flags currently raised on one project.

    Args:
        project_id: Numeric primary key of the project.

    Returns:
        The raised flags, oldest detection first, so the UI can say how long each has been true.
    """
    return list(
        RiskFlag.objects.filter(project_id=project_id, cleared_at__isnull=True).order_by(
            "detected_at"
        )
    )


def live_override(project_id: int) -> PriorityOverride | None:
    """The override in force on one project, if any.

    "In force" is ``revoked_at IS NULL``; expiry is evaluated by the caller against its own
    ``now``, because a service must not read a clock this function chose.

    Args:
        project_id: Numeric primary key of the project.

    Returns:
        The live override row, or ``None``.
    """
    return PriorityOverride.objects.filter(project_id=project_id, revoked_at__isnull=True).first()
