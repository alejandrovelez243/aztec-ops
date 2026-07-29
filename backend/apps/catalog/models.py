"""Persistence for the configurable taxonomies (``DATA_MODEL`` §1).

The six tables here are the operation's vocabulary: engagement types, project types, stages,
priorities, roles and currencies. They are rows rather than Python enums so the operation can
rename, recolor, reorder or retire a value from the admin without a deploy, which is why nothing in
this module carries a business rule — the rules read ``code``, ``weight`` or ``is_urgent`` from
these rows.

Named queries live on :class:`TaxonomyQuerySet` and are exposed through each taxonomy's manager
(CLAUDE.md rule 6): the six tables share one abstract base, so they share one queryset and
``EngagementType.objects.active().indexed_by_code()`` means the same thing everywhere. Beyond the
queryset this module holds fields, constraints, indexes and each row's projection of itself.
"""

from decimal import Decimal
from typing import ClassVar, Self

from django.db import models

from apps.catalog.domain.errors import UnknownCode
from apps.catalog.domain.value_objects import CurrencyRef
from apps.shared.refs import TaxonomyRef

#: Default multiplier for the taxonomies the prioritization engine reads. A weight of 1.00 is the
#: neutral element of the modifier product, so a freshly created row cannot silently move a score.
NEUTRAL_WEIGHT = Decimal("1.00")

#: Shape of an ISO-4217 alphabetic currency code: exactly three upper-case letters. Enforced in the
#: database rather than only in a form, because ``loaddata`` and the ORM both bypass form cleaning.
ISO_4217_CODE_PATTERN = r"^[A-Z]{3}$"

#: Decimal places of the currencies most of the world bills in, and therefore the safe default for
#: a newly created row: getting it wrong is a rendering error of two orders of magnitude.
DEFAULT_MINOR_UNITS = 2

#: The largest exponent ISO-4217 defines (CLF, the Chilean unidad de fomento). A larger value is
#: not a currency the standard knows, so the database refuses it instead of letting a client
#: render an amount nobody can reconcile.
MAX_MINOR_UNITS = 4


class TaxonomyQuerySet[TaxonomyT: "TaxonomyBase"](models.QuerySet[TaxonomyT]):
    """The questions every taxonomy is asked, written once for all six tables.

    Generic over the concrete model so a chain keeps its type: ``Stage.objects.active()`` is a
    queryset of :class:`Stage`, not of the abstract base, and mypy rejects reading a field the
    concrete table does not have.

    Ordering is not restated here: :class:`TaxonomyBase.Meta` already orders by ``(order, code)``,
    with ``code`` breaking ties so two rows sharing an ``order`` cannot swap places between
    requests and make the picker look like it is flickering.
    """

    def active(self) -> Self:
        """The rows an operator may pick today.

        Retirement is ``is_active = False``, never a delete, so this is the picker's view and not
        the history's: rows already referenced by a project keep resolving through their foreign
        key. Served by the ``(is_active, order)`` index.
        """
        return self.filter(is_active=True)

    def find_by_code(self, code: str) -> TaxonomyT | None:
        """Resolve a slug, or ``None`` when this taxonomy carries no such row.

        For the callers whose whole job is to decide whether a code exists — a fixture check, an
        admin form — where absence is an answer rather than a failure. Materialises: it ends the
        chain.

        Args:
            code: Stable ASCII slug, e.g. ``"proyecto"``.

        Returns:
            The matching row, or ``None``.
        """
        return self.filter(code=code).first()

    def by_code(self, code: str) -> TaxonomyT:
        """Resolve a slug to its row, refusing to carry a missing one forward.

        Logic compares against ``code``, so a code that does not resolve is a broken contract
        rather than an empty result; returning ``None`` here would let a caller carry a missing
        engagement type into a score, where it becomes a wrong number instead of an error.

        Called on the unfiltered manager it resolves retired rows too, on purpose: a project
        created last quarter still points at an engagement type that has since been deactivated,
        and refusing to read it would break the history rather than the picker. Chain
        :meth:`active` first when only a currently pickable row will do. Materialises: it ends the
        chain.

        Args:
            code: Stable ASCII slug.

        Returns:
            The matching row.

        Raises:
            UnknownCode: No row in this taxonomy carries that code.
        """
        row = self.find_by_code(code)
        if row is None:
            raise UnknownCode(str(self.model._meta.verbose_name), code)
        return row

    def indexed_by_code(self) -> dict[str, TaxonomyT]:
        """Index the selected rows by their slug.

        The shape a batch job wants: resolving 82 tasks against four priorities is one query plus
        dictionary lookups instead of 82 round trips. Materialises: it ends the chain.

        Returns:
            Mapping of ``code`` to row, over whatever the chain selected.
        """
        return {row.code: row for row in self}

    def codes(self) -> frozenset[str]:
        """The slugs of the selected rows, for membership tests.

        Materialises: it ends the chain.

        Returns:
            The codes, deduplicated; empty when the chain selected nothing.
        """
        return frozenset(self.values_list("code", flat=True))


class PriorityQuerySet(TaxonomyQuerySet["Priority"]):
    """The taxonomy questions, plus the one only priorities are asked."""

    def urgent(self) -> Self:
        """The priorities the ``criticality`` signal counts at all.

        Read from ``is_urgent`` rather than from a literal set of codes, so adding or renaming a
        priority is a fixture row and never a Python change (``DATA_MODEL`` §12).
        """
        return self.filter(is_urgent=True)


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

    def to_ref(self) -> TaxonomyRef:
        """Describe this row as the ``{code, label, color}`` every read surface renders.

        Issues no query: it reads three of its own columns. Lives on the model because the row owns
        the mapping of its own fields (CLAUDE.md rule 6) — a free ``_to_ref(row)`` in a service
        would fragment the moment a second read surface needed the same projection.

        Returns:
            The reference, with an unset color normalized to ``None``.
        """
        return TaxonomyRef.of(code=self.code, label=self.label, color=self.color)


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

    #: Parameterised with the concrete model so a chain keeps its type: the shared queryset is
    #: generic, and an unsubscripted ``as_manager()`` would resolve every row to ``TaxonomyBase``.
    objects = TaxonomyQuerySet["EngagementType"].as_manager()

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

    objects = TaxonomyQuerySet["ProjectType"].as_manager()

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

    objects = TaxonomyQuerySet["Stage"].as_manager()

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

    #: Overrides the inherited manager with the one that also knows :meth:`PriorityQuerySet.urgent`.
    objects = PriorityQuerySet.as_manager()

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


class Currency(TaxonomyBase):
    """The currency a project is billed in — an ISO-4217 alphabetic code and how it is written.

    A taxonomy rather than a ``CharField`` on ``Project`` for the reason every other taxonomy is
    one: the frontend renders a validated select, and the only way to do that without hardcoding a
    list in the client is to serve it. A free-text column would also accept ``"usd"``,
    ``"US$"`` and ``"Dolar"`` as three different currencies.

    ``minor_units`` is not decoration. It is the number of decimal places the amount is written
    with, and it is a property of the currency rather than of the formatter: JPY and CLP have 0,
    USD and EUR have 2, so ``28000`` is ¥28,000 in one and $280.00 in the other. A client that
    guessed 2 everywhere would render a Chilean contract a hundred times too small. Constrained to
    the 0-4 the standard actually uses (CLF, the Chilean unidad de fomento, is the 4).

    ``code`` is the ISO alphabetic code in upper case, checked in the database: the taxonomy is
    interoperable vocabulary here, not an operator-invented slug, so ``eur`` and ``EURO`` are
    rejected at write time rather than discovered later by a client that could not match them.
    """

    minor_units = models.PositiveSmallIntegerField(default=DEFAULT_MINOR_UNITS)

    objects = TaxonomyQuerySet["Currency"].as_manager()

    class Meta(TaxonomyBase.Meta):
        """Adds the ISO-4217 shape check and the minor-unit range to the base constraints."""

        verbose_name = "currency"
        verbose_name_plural = "currencies"
        constraints = (
            *TaxonomyBase.Meta.constraints,
            models.CheckConstraint(
                condition=models.Q(code__regex=ISO_4217_CODE_PATTERN),
                name="catalog_currency_code_is_iso_4217",
            ),
            models.CheckConstraint(
                condition=models.Q(minor_units__lte=MAX_MINOR_UNITS),
                name="catalog_currency_minor_units_within_iso_range",
            ),
        )

    def to_currency_ref(self) -> CurrencyRef:
        """Describe this row as the reference a client needs to *format* an amount.

        Separate from :meth:`TaxonomyBase.to_ref` because the extra field is load-bearing: a caller
        handed a plain ``{code, label}`` has to guess the decimal places, and the guess is wrong for
        every zero-decimal currency. Issues no query.

        Returns:
            The reference, with an unset color normalized to ``None``.
        """
        return CurrencyRef(
            code=self.code,
            label=self.label,
            color=self.color or None,
            minor_units=self.minor_units,
        )


class Role(TaxonomyBase):
    """Team member role — Delivery, Commercial / Delivery.

    Foreign key target of ``accounts.User.role`` with ``on_delete=SET_NULL``: a person outlives
    the role they were classified under, so deleting a role must not delete the person — and the
    person is now the account itself, since whoever is assigned a task is whoever signs in to
    move it.
    """

    objects = TaxonomyQuerySet["Role"].as_manager()

    class Meta(TaxonomyBase.Meta):
        """Base shape unchanged."""

        verbose_name = "role"
