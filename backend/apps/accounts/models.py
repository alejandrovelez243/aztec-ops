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
  is :func:`apps.portfolio.repositories.owner_load_for_codes` — "who is overloaded?" is a
  portfolio question, and ``accounts`` must not learn that ``work.Task`` exists. It is the one
  query in this codebase that belongs to no single model, which is why it is the one that is
  not a manager method.
* **``ActivityRecord.actor`` remains a string**, not a foreign key, precisely so the
  prioritization engine and the stream consumers can write records as ``system`` without a fake
  user row existing to satisfy a constraint.

Seed people have no credentials: the fixtures call ``set_unusable_password()``. They are real
assignees who simply have no password until someone sets one, which is exactly what that method
exists for.
"""

from collections.abc import Sequence
from typing import ClassVar, Self

from django.contrib.auth.models import AbstractUser
from django.contrib.auth.models import UserManager as AuthUserManager
from django.db import models

from apps.shared.refs import ActorRef

#: Default weekly capacity of a person, in points. It is the divisor of owner load, so the
#: database refuses zero (DATA_MODEL §9.2) and the default is a working week's worth of tasks.
DEFAULT_WEEKLY_CAPACITY_POINTS = 20


class UserQuerySet(models.QuerySet["User"]):
    """The named reads of the identity context, composable in any order.

    "Resolve a person by code" was written twice before the merge — once in ``portfolio`` and
    once in ``work`` — and definitions written twice drift. It has one home now, and because it
    is a queryset method the callers that need something narrower (``active()``, a specific
    ordering, a further filter) refine it instead of asking for a new function.
    """

    def with_role(self) -> "UserQuerySet":
        """Load each person's role in the same query.

        Role is rendered wherever a person is rendered, so leaving it lazy turns a roster
        listing into one query per row. Opt-in rather than automatic: an owner-load aggregation
        reads only ``weekly_capacity_points`` and should not pay for the join.
        """
        return self.select_related("role")

    def by_code(self, code: str) -> "UserQuerySet":
        """Narrow to the person carrying this stable code.

        Inactive people still match: deactivation retires someone from new assignment, it does
        not erase the tasks and projects that already name them, and a caller rendering those
        needs the row. A caller that means "someone who can take work" chains ``active()``.

        Args:
            code: ``User.code``, e.g. ``"camila.torres"``.
        """
        return self.filter(code=code)

    def by_codes(self, codes: Sequence[str]) -> "UserQuerySet":
        """Narrow to the people carrying any of these codes.

        Exists so a caller holding N codes — a projection builder, a bulk import — resolves them
        in one query. An empty sequence yields no rows rather than every row, because "resolve
        nothing" must not silently mean "resolve everybody".

        Args:
            codes: ``User.code`` values.
        """
        return self.filter(code__in=list(codes))

    def active(self) -> "UserQuerySet":
        """Narrow to the assignable roster.

        ``is_active`` is inherited from ``AbstractUser`` and means "still part of the
        operation"; a second flag for the roster would be the same fact stored twice.
        """
        return self.filter(is_active=True)

    def keyed_by_code(self) -> dict[str, "User"]:
        """Materialise the selection as a mapping of code to person, ending the chain.

        Codes matching no row are simply absent rather than mapped to ``None``: absence is the
        answer, and a key present with a null value would invite callers to treat "unknown
        person" as "person with no data".
        """
        return {user.code: user for user in self}


#: The roster's queries plus the account-creation behaviour Django's auth machinery needs. Built
#: with ``from_queryset`` rather than ``UserQuerySet.as_manager()`` because this manager must keep
#: ``create_user`` / ``create_superuser``: ``manage.py createsuperuser`` and the ``AUTH_USER_MODEL``
#: contract call them, and they are not queryset methods.
#:
#: It is the one manager in the codebase that is *model state*: Django's
#: :class:`~django.contrib.auth.models.UserManager` sets ``use_in_migrations = True``, so
#: ``accounts/0001_initial`` records it by import path and the migration writer must be able to
#: find it again. ``from_queryset`` builds the class inside Django's own module under a derived
#: name, which no import path reaches; naming it and re-homing ``__module__`` here makes the class
#: genuinely be this module's ``UserManager``, so ``deconstruct()`` resolves and changing the
#: queryset behind it writes no migration — the rule that holds everywhere else.
UserManager = AuthUserManager.from_queryset(UserQuerySet, class_name="UserManager")
UserManager.__module__ = __name__


class User(AbstractUser):
    """A person who owns projects, is assigned tasks, and can sign in.

    ``code`` is the stable slug every payload carries — the event bus, the fixtures and the
    snapshot read model all refer to a person by it, never by primary key and never by
    ``alias``, which is operator-editable display text. ``username`` mirrors ``code`` so
    Django's authentication machinery and the business identifier cannot drift apart.

    ``weekly_capacity_points`` is the *denominator* of owner load. The numerator is never
    stored: it is derived from ``work.Task`` rows by
    :func:`apps.portfolio.repositories.owner_load_for_codes`. The source ``Team`` sheet's
    counters are a stale projection and are deliberately not imported (DATA_MODEL §3).

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

    objects: ClassVar["UserManager[Self]"] = UserManager()

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

    @property
    def is_ops_lead(self) -> bool:
        """Whether this person may override the ranking and rebuild the whole portfolio.

        **Why ``is_staff`` and not the ``role`` foreign key.** ``catalog.Role`` is operator-editable
        taxonomy: its rows are renamed, reordered and retired from the admin like every other
        taxonomy (CLAUDE.md rule 1). Hanging authorization off it would mean that retiring a role,
        or a typo in its ``code``, silently revokes a permission — a label edit becoming a security
        change. Mapping it onto a Django ``Group`` instead would fix the fragility and cost a
        migration, a permission codename, a fixture, and a synchronisation rule to keep group
        membership agreeing with the role column: real machinery whose only readers are the two
        ``if``s this property serves. ``is_staff`` is already on
        :class:`~django.contrib.auth.models.AbstractUser`, is identity rather than taxonomy, is
        editable from the admin, and needs no new table.

        The overlap with admin access is a feature, not a coincidence: the person trusted to edit
        workflows and priorities from ``/admin/`` is the person trusted to force a rank against the
        engine. If the two ever have to diverge, that is the moment to introduce the group — and
        this property is the single place that changes.

        Returns:
            ``True`` when this account may take the two portfolio-wide actions.
        """
        return self.is_staff

    def to_ref(self) -> ActorRef:
        """Describe this person as the reference shape every payload that names a person renders.

        A method on the model rather than a converter in a router, for the reason CLAUDE.md rule 6
        gives: a free function reading four attributes off a ``User`` it was handed fragments the
        moment a second caller needs the same projection, and there are five of them here — the
        queue's owner, a task's assignee, a blocker's owner, a note's author and the team-load row.

        ``role`` is read through the relation, so a caller rendering many people chains
        :meth:`UserQuerySet.with_role` first; on an instance fetched without it this is still
        correct but costs one query per person.

        Returns:
            The person as an immutable :class:`~apps.shared.refs.ActorRef`, whose ``alias`` is the
            stable ``code`` and whose ``label`` is the operator-editable display name.
        """
        return ActorRef.of(
            code=self.code,
            label=self.alias,
            role=self.role.label if self.role else None,
        )
