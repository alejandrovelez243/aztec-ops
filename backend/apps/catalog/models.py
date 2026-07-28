"""Persistence for the configurable taxonomies (``DATA_MODEL`` §1).

The five tables here are the operation's vocabulary: engagement types, project types, stages,
priorities and roles. They are rows rather than Python enums so the operation can rename, recolor,
reorder or retire a value from the admin without a deploy, which is why nothing in this module
carries a business rule — the rules read ``code``, ``weight`` or ``is_urgent`` from these rows.

Queries live in :mod:`apps.catalog.repositories`; this module holds fields, constraints and
indexes only.
"""

from decimal import Decimal
from typing import ClassVar

from django.db import models

#: Default multiplier for the taxonomies the prioritization engine reads. A weight of 1.00 is the
#: neutral element of the modifier product, so a freshly created row cannot silently move a score.
NEUTRAL_WEIGHT = Decimal("1.00")


class TaxonomyBase(models.Model):
    """Shared shape of every ``catalog`` table: a stable slug plus its presentation.

    ``code`` is the contract with the code base and is the only column logic ever compares
    against; ``label`` is Spanish, operator-editable data, and comparing against it would break the
    first time somebody fixes a typo in the admin. ``is_active`` is a soft retirement: an inactive
    row disappears from pickers but keeps resolving on the foreign keys that already point at it,
    so retiring a taxonomy value can never orphan a project.

    Concrete subclasses inherit the unique ``code`` constraint and the ``(is_active, order)``
    index through the ``%(app_label)s_%(class)s`` naming placeholders.
    """

    code = models.CharField(max_length=32)
    label = models.CharField(max_length=64)
    order = models.SmallIntegerField(default=0)
    is_active = models.BooleanField(default=True)
    color = models.CharField(max_length=7, blank=True, default="")

    class Meta:
        """Shape shared by the concrete tables: ordering, unique code, activity index."""

        abstract = True
        ordering = ("order", "code")
        # Annotated wide on purpose: a concrete taxonomy extends this tuple with its own checks
        # (see ``EngagementType.Meta``), and an inferred one-element type would forbid that.
        constraints: ClassVar[tuple[models.BaseConstraint, ...]] = (
            models.UniqueConstraint(
                fields=("code",),
                name="%(app_label)s_%(class)s_code_unique",
            ),
        )
        indexes = (
            models.Index(
                fields=("is_active", "order"),
                name="%(app_label)s_%(class)s_act_ord",
            ),
        )

    def __str__(self) -> str:
        """Show the operator-facing label; the code is an implementation detail to them."""
        return self.label


class EngagementType(TaxonomyBase):
    """How an engagement behaves commercially and in the ranking.

    ``weight`` is a *multiplier* applied to the weighted signal sum (``ARCHITECTURE`` §4.1), never
    an addend, which is what keeps a short Diagnostico from being permanently starved by a large
    Proyecto without inflating any individual signal. A zero or negative weight would silently
    erase every signal's effect, so the database refuses it.

    Also the join key of ``WorkflowBinding``: the engagement type decides which workflow a project
    follows.
    """

    weight = models.DecimalField(max_digits=4, decimal_places=2, default=NEUTRAL_WEIGHT)

    class Meta(TaxonomyBase.Meta):
        """Adds the positive-weight check to the inherited base constraints."""

        verbose_name = "engagement type"
        constraints = (
            *TaxonomyBase.Meta.constraints,
            models.CheckConstraint(
                condition=models.Q(weight__gt=0),
                name="catalog_engagementtype_weight_positive",
            ),
        )


class ProjectType(TaxonomyBase):
    """The dataset's ``project_type_api`` — Automatizacion, Consultoria.

    A pure classification: it carries no weight and nothing branches on it, so it exists to be
    filtered and displayed. Optional on ``Project``, because the source leaves it empty.
    """

    class Meta(TaxonomyBase.Meta):
        """Base shape unchanged; only the admin naming differs."""

        verbose_name = "project type"


class Stage(TaxonomyBase):
    """Delivery stage — Descubrimiento, Ejecucion.

    ``order`` is load-bearing here rather than cosmetic: the source ``status`` column is ``Activo``
    on all 22 projects, so the fixture generator maps the stage onto the project's initial
    ``WorkflowState`` by this ordering. Reordering stages in the admin therefore changes what a
    future seed produces, and nothing already persisted.
    """

    class Meta(TaxonomyBase.Meta):
        """Base shape unchanged; the inherited ordering is the delivery order."""

        verbose_name = "stage"


class Priority(TaxonomyBase):
    """Task priority — Critica, Alta, Media, Baja.

    Two columns beyond the base, answering two different questions. ``weight`` is the numeric
    severity the ``criticality`` signal reads; ``is_urgent`` is the structural fact marking which
    codes that signal counts at all. Keeping ``is_urgent`` as data is what lets the operation add a
    priority above ``Critica`` without a migration and without a grep for hardcoded codes.

    As with :class:`EngagementType`, a non-positive weight is rejected in the database because it
    would remove the signal's effect while looking like a valid row.
    """

    weight = models.DecimalField(max_digits=4, decimal_places=2, default=NEUTRAL_WEIGHT)
    is_urgent = models.BooleanField(default=False)

    class Meta(TaxonomyBase.Meta):
        """Adds the positive-weight check, and the plural Django cannot guess."""

        verbose_name = "priority"
        verbose_name_plural = "priorities"
        constraints = (
            *TaxonomyBase.Meta.constraints,
            models.CheckConstraint(
                condition=models.Q(weight__gt=0),
                name="catalog_priority_weight_positive",
            ),
        )


class Role(TaxonomyBase):
    """Team member role — Delivery, Commercial / Delivery.

    Foreign key target of ``accounts.User.role`` with ``on_delete=SET_NULL``: a person outlives
    the role they were classified under, so deleting a role must not delete the person — and the
    person is now the account itself, since whoever is assigned a task is whoever signs in to
    move it.
    """

    class Meta(TaxonomyBase.Meta):
        """Base shape unchanged."""

        verbose_name = "role"
