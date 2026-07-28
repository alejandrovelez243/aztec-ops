"""Persistence for the prioritization context: policy, score, override and risk flag.

Fields, constraints, indexes and the named queries of each table. The arithmetic lives in
``domain/``; a business rule here would be a rule that also runs on every instance the ORM builds
during an unrelated query.

Two definitions live on these querysets and nowhere else: "open flag" is ``cleared_at IS NULL``
and "live override" is ``revoked_at IS NULL``. Re-deriving either in a service, a consumer and the
admin is how the three come to disagree.
"""

from datetime import datetime

from django.db import models

from .domain.types import Severity
from .domain.views import OverrideView, RiskFlagView, ScoreView

#: Severity choices for ``RiskFlag``, derived from the domain enum so the database and the
#: registry can never disagree about the four values that exist.
SEVERITY_CHOICES = [(severity.value, severity.value.title()) for severity in Severity]


class PriorityPolicyQuerySet(models.QuerySet["PriorityPolicy"]):
    """Named reads of the scoring criterion."""

    def active(self) -> "PriorityPolicyQuerySet":
        """Narrow to the policy currently in force.

        A partial unique index guarantees at most one row matches, so the caller ends this with
        ``.first()`` and raises ``ActivePolicyNotFound`` on ``None`` rather than scoring against
        an implicit default.
        """
        return self.filter(is_active=True)


class PriorityScoreQuerySet(models.QuerySet["PriorityScore"]):
    """Named reads of the computed ranking."""

    def for_project(self, project_id: int) -> "PriorityScoreQuerySet":
        """Narrow to one project's score row.

        Args:
            project_id: Numeric primary key of the project.
        """
        return self.filter(project=project_id)

    def stale_at(self, moment: datetime) -> "PriorityScoreQuerySet":
        """Narrow to the scores whose time-dependent signals have crossed a bucket boundary.

        This is the whole cost of a clock tick: one scan of ``prioritization_score_valid``,
        ordered by the same column so the plan carries no sort node. A quiet tick matches
        nothing and nothing is recomputed. Rows with no ``valid_until`` never expire and are
        excluded rather than treated as due.

        Args:
            moment: The tick instant.
        """
        return self.filter(valid_until__isnull=False, valid_until__lte=moment).order_by(
            "valid_until"
        )

    def project_ids(self) -> list[int]:
        """Materialise the selection as project ids, ending the chain.

        Returned instead of the rows because the caller enqueues recomputations, and holding
        score rows across that hand-off would carry values that the recomputation is about to
        replace.
        """
        return list(self.values_list("project_id", flat=True))

    def project_codes(self) -> list[str]:
        """Materialise the selection as project business codes, ending the chain.

        The clock tick's second half: :meth:`stale_at` names the rows, this names the projects a
        consumer can act on. Codes rather than ids because every use case, event envelope and
        audit record downstream is addressed by ``Project.code``, so returning ids would make the
        consumer the one caller that has to resolve them.
        """
        return [str(code) for code in self.values_list("project__code", flat=True)]


class PriorityOverrideQuerySet(models.QuerySet["PriorityOverride"]):
    """Named reads of the manual forcings."""

    def for_project(self, project_id: int) -> "PriorityOverrideQuerySet":
        """Narrow to one project's overrides, revoked ones included.

        Args:
            project_id: Numeric primary key of the project.
        """
        return self.filter(project=project_id)

    def live(self) -> "PriorityOverrideQuerySet":
        """Narrow to the overrides still in force.

        "In force" is ``revoked_at IS NULL``. Expiry is deliberately not evaluated here: a
        query must not read a clock its caller did not choose, so the service compares
        ``expires_at`` against its own ``now``.
        """
        return self.filter(revoked_at__isnull=True)


class RiskFlagQuerySet(models.QuerySet["RiskFlag"]):
    """Named reads of the risk detections."""

    def for_project(self, project_id: int) -> "RiskFlagQuerySet":
        """Narrow to one project's flags, cleared episodes included.

        Args:
            project_id: Numeric primary key of the project.
        """
        return self.filter(project=project_id)

    def open(self) -> "RiskFlagQuerySet":
        """Narrow to the flags currently raised.

        ``cleared_at IS NULL`` is the definition of "raised", and it is the condition of the
        ``prioritization_open_flags`` partial index, so this selection never touches a cleared
        row.
        """
        return self.filter(cleared_at__isnull=True)

    def oldest_first(self) -> "RiskFlagQuerySet":
        """Order by detection, earliest first, so the UI can say how long each has been true."""
        return self.order_by("detected_at")

    def as_views(self) -> tuple[RiskFlagView, ...]:
        """Materialise the selection as the projections the risk panels render.

        Ends the chain: the query executes here, so a caller cannot narrow the flag set after the
        response shape was decided.
        """
        return tuple(flag.to_view() for flag in self)


class PriorityPolicy(models.Model):
    """A versioned set of signal weights: the criterion the ranking is computed from.

    Exactly one row is active, enforced by a partial unique index. Changing weights means
    inserting a new version and moving ``is_active``: editing the active row silently rewrites the
    meaning of every score already persisted against it, which is how a ranking stops being
    auditable.
    """

    version = models.CharField(max_length=16, unique=True)
    is_active = models.BooleanField(default=False)
    weights = models.JSONField()
    modifiers = models.JSONField(default=dict, blank=True)
    notes = models.TextField(default="", blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    objects = PriorityPolicyQuerySet.as_manager()

    class Meta:
        verbose_name = "priority policy"
        verbose_name_plural = "priority policies"
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["is_active"],
                condition=models.Q(is_active=True),
                name="prioritization_one_active_policy",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.version}{' (active)' if self.is_active else ''}"


class PriorityScore(models.Model):
    """The current computed score of one project, with the document that justifies it.

    One row per project, overwritten on recompute: history lives in ``ActivityRecord`` with verb
    ``PRIORITY_CHANGED``, and a second history table here could only ever disagree with it.
    ``policy_version`` is a copy rather than a foreign key so a score stays readable after its
    policy is retired.
    """

    project = models.OneToOneField(
        "portfolio.Project",
        on_delete=models.CASCADE,
        related_name="priority_score",
    )
    value = models.DecimalField(max_digits=5, decimal_places=2)
    policy_version = models.CharField(max_length=16)
    breakdown = models.JSONField()
    modifier_total = models.DecimalField(max_digits=4, decimal_places=2, default=1)
    computed_at = models.DateTimeField()
    input_hash = models.CharField(max_length=64, default="", blank=True)
    valid_until = models.DateTimeField(null=True, blank=True)

    objects = PriorityScoreQuerySet.as_manager()

    class Meta:
        verbose_name = "priority score"
        ordering = ["-value"]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(value__gte=0) & models.Q(value__lte=100),
                name="prioritization_score_within_range",
            ),
        ]
        indexes = [
            models.Index(fields=["-value"], name="prioritization_score_desc"),
            models.Index(fields=["policy_version"], name="prioritization_score_policy"),
            # The clock tick's only query: WHERE valid_until <= tick_at. One index scan, and a
            # quiet tick produces nothing.
            models.Index(fields=["valid_until"], name="prioritization_score_valid"),
        ]

    def __str__(self) -> str:
        return f"{self.project_id}: {self.value} ({self.policy_version})"

    def to_view(self) -> ScoreView:
        """Describe this score as the projection the API returns, argument included.

        The persisted ``value`` and ``policy_version`` columns win over the copies inside
        ``breakdown``: those columns are what the queue ordered by and what the range check
        constrained, so a document that disagreed with them must not be the version the client
        sees. Issues no query.

        Returns:
            The score as an immutable :class:`~apps.prioritization.domain.views.ScoreView`.
        """
        return ScoreView.from_document(
            self.breakdown,
            value=self.value,
            policy_version=self.policy_version,
        )


class PriorityOverride(models.Model):
    """A human forcing a position or nudging a score, with the reason on the record.

    Never written into ``PriorityScore.value``. The computed score stays visible beside it, the UI
    labels the row as an override, and revoking restores the ranking with no recompute. ``reason``
    is mandatory at the database level because a forced position with no recorded reason is
    indistinguishable from a bug three weeks later.
    """

    project = models.ForeignKey(
        "portfolio.Project",
        on_delete=models.CASCADE,
        related_name="priority_overrides",
    )
    position = models.SmallIntegerField(null=True, blank=True)
    boost = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)
    reason = models.CharField(max_length=500)
    actor = models.CharField(max_length=32)
    expires_at = models.DateTimeField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    objects = PriorityOverrideQuerySet.as_manager()

    class Meta:
        verbose_name = "priority override"
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["project"],
                condition=models.Q(revoked_at__isnull=True),
                name="prioritization_one_live_override",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(position__isnull=False, boost__isnull=True)
                    | models.Q(position__isnull=True, boost__isnull=False)
                ),
                name="prioritization_override_one_mechanism",
            ),
            models.CheckConstraint(
                condition=models.Q(reason__regex=r"\S"),
                name="prioritization_override_reason_present",
            ),
        ]
        indexes = [
            models.Index(fields=["expires_at"], name="prioritization_override_expiry"),
        ]

    def __str__(self) -> str:
        mechanism = (
            f"position {self.position}" if self.position is not None else f"boost {self.boost}"
        )
        return f"{self.project_id}: {mechanism} by {self.actor}"

    def to_view(self) -> OverrideView:
        """Describe this override as the projection the queue and the detail view render.

        ``revoked_at`` is deliberately not exposed: a revoked override is simply absent from the
        response, and shipping the column would invite a client to render a decision that is no
        longer in force. Issues no query.

        Returns:
            The override as an immutable :class:`~apps.prioritization.domain.views.OverrideView`.
        """
        return OverrideView(
            position=self.position,
            boost=float(self.boost) if self.boost is not None else None,
            reason=self.reason,
            actor=self.actor,
            created_at=self.created_at,
            expires_at=self.expires_at,
        )


class RiskFlag(models.Model):
    """A persisted risk detection, raised and cleared as separate rows.

    A flag going from raised to cleared and back produces two rows, never one mutated row, so the
    risk history is reconstructible and ``detected_at`` keeps meaning "since when" — that is what
    lets the UI say "blocked for 19 days". ``severity`` is written from the registry entry, not
    chosen per row.
    """

    project = models.ForeignKey(
        "portfolio.Project",
        on_delete=models.CASCADE,
        related_name="risk_flags",
    )
    code = models.CharField(max_length=32)
    severity = models.CharField(max_length=8, choices=SEVERITY_CHOICES)
    detail = models.CharField(max_length=255, default="", blank=True)
    detected_at = models.DateTimeField()
    cleared_at = models.DateTimeField(null=True, blank=True)

    objects = RiskFlagQuerySet.as_manager()

    class Meta:
        verbose_name = "risk flag"
        ordering = ["-detected_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["project", "code"],
                condition=models.Q(cleared_at__isnull=True),
                name="prioritization_one_open_flag_per_code",
            ),
            models.CheckConstraint(
                condition=models.Q(cleared_at__isnull=True)
                | models.Q(cleared_at__gte=models.F("detected_at")),
                name="prioritization_flag_cleared_after_detected",
            ),
        ]
        indexes = [
            # The open-flags panels and the snapshot rebuild read only raised flags; a cleared row
            # is never read by either, so the index does not carry them.
            models.Index(
                fields=["project"],
                condition=models.Q(cleared_at__isnull=True),
                name="prioritization_open_flags",
            ),
            models.Index(fields=["severity"], name="prioritization_flag_severity"),
            models.Index(fields=["detected_at"], name="prioritization_flag_detected"),
        ]

    def __str__(self) -> str:
        return f"{self.project_id}: {self.code} ({self.severity})"

    def to_view(self) -> RiskFlagView:
        """Describe this flag as the projection the risk panels render.

        ``detail`` is published as ``reason`` because that is the name `docs/API.md` §1.6 fixed on
        the wire, and because it is what the field is: the sentence naming the fact that raised the
        flag. Issues no query.

        Returns:
            The flag as an immutable :class:`~apps.prioritization.domain.views.RiskFlagView`.
        """
        return RiskFlagView(code=self.code, severity=self.severity, reason=self.detail)
