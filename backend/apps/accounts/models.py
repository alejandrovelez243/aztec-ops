"""Identity: the one row per person in the operation.

``AUTH_USER_MODEL`` cannot be changed after the first migration without hand-written surgery
across every table that references it, so a custom user exists on day one. What it carries is
the decision this module records.

**A person is one entity.** The person a task is assigned to is the person who signs in to move
it, so ``portfolio.TeamMember`` is merged into :class:`User` rather than kept beside it. The
previous design gave ``TeamMember`` a nullable one-to-one to the account; a nullable link that
is never null lies in the schema, and it forces a ``request.user.team_member`` hop that can be
``None`` at every permission check — a branch nobody can ever remove because the type says it
is reachable.

The cost is written down rather than hidden: identity now carries two operational attributes,
``role`` and ``weekly_capacity_points``. One row per person beats a nullable one-to-one that is
never null, and the alternative — a separate profile table joined on every read — buys purity
we would pay for on every query.

Two things did *not* move here:

* **Owner load stays where it is consumed.** ``weekly_capacity_points`` lives on the person
  because it describes the person, but the query deriving the numerator from ``work.Task`` rows
  stays in ``apps.portfolio.repositories`` — "who is overloaded?" is a portfolio question, and
  ``accounts`` must not learn about ``work.Task``.
* **``ActivityRecord.actor`` remains a string**, not a foreign key, precisely so the
  prioritization engine and the stream consumers can write records as ``system`` without a fake
  user row existing to satisfy a constraint.

Seed people have no credentials: the fixtures call ``set_unusable_password()``. They are real
assignees who simply have no password until someone sets one, which is exactly what that method
exists for.
"""

from django.contrib.auth.models import AbstractUser
from django.db import models

#: Default weekly capacity of a person, in points. It is the divisor of owner load, so the
#: database refuses zero (DATA_MODEL §9.2) and the default is a working week's worth of tasks.
DEFAULT_WEEKLY_CAPACITY_POINTS = 20


class User(AbstractUser):
    """A person who owns projects, is assigned tasks, and can sign in.

    ``code`` is the stable slug every payload carries — the event bus, the fixtures and the
    snapshot read model all refer to a person by it, never by primary key and never by
    ``alias``, which is operator-editable display text. ``username`` mirrors ``code`` so
    Django's authentication machinery and the business identifier cannot drift apart.

    ``weekly_capacity_points`` is the *denominator* of owner load. The numerator is never
    stored: it is derived from ``work.Task`` rows by
    :meth:`apps.portfolio.repositories.OwnerLoadRepository.load_for_codes`. The source ``Team``
    sheet's counters are a stale projection and are deliberately not imported (DATA_MODEL §3).

    ``is_active`` comes from :class:`~django.contrib.auth.models.AbstractUser` and means
    "still part of the operation"; a second flag for the roster would be the same fact stored
    twice.
    """

    code = models.CharField(max_length=32)
    alias = models.CharField(max_length=96)
    role = models.ForeignKey(
        "catalog.Role",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="members",
    )
    weekly_capacity_points = models.SmallIntegerField(default=DEFAULT_WEEKLY_CAPACITY_POINTS)

    class Meta(AbstractUser.Meta):
        verbose_name = "user"
        verbose_name_plural = "users"
        db_table = "accounts_user"
        ordering = ["alias"]
        constraints = [
            models.UniqueConstraint(fields=["code"], name="accounts_user_code_unique"),
            models.CheckConstraint(
                condition=models.Q(weekly_capacity_points__gt=0),
                name="accounts_user_capacity_positive",
            ),
        ]

    def __str__(self) -> str:
        """Return the display alias."""
        return self.alias
