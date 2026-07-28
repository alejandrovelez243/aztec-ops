"""Every query the catalog context answers.

Five tables with an identical shape means five identical query sets, so there is one generic
repository parameterised by the concrete model and one module-level instance per taxonomy. Callers
in other contexts import the instance (``engagement_types.get_by_code("proyecto")``) and never
build a queryset of their own — that is what keeps "active, in display order" meaning one thing.

Lookups come in two flavours on purpose: ``get_by_code`` raises :class:`UnknownCode` because a code
that does not resolve is a broken contract, and ``find_by_code`` returns ``None`` for the callers
whose whole job is to decide whether a slug exists.
"""

from apps.catalog.domain.errors import UnknownCode
from apps.catalog.models import (
    EngagementType,
    Priority,
    ProjectType,
    Role,
    Stage,
    TaxonomyBase,
)


class TaxonomyRepository[TaxonomyT: TaxonomyBase]:
    """Reads one taxonomy table.

    Every method materialises its result — a returned ``QuerySet`` would move the query back into
    the caller's layer and make the annotation a lie.

    Queries go through ``_default_manager`` rather than ``objects``: ``objects`` is attached to
    each *concrete* taxonomy, so it does not exist on the abstract bound of ``TaxonomyT``, while
    ``_default_manager`` is Django's documented accessor for exactly this case — code that holds a
    model class it did not name and must not assume how its manager is called.

    Attributes:
        taxonomy: Human name used in error messages, e.g. ``"engagement type"``.
    """

    def __init__(self, model: type[TaxonomyT], taxonomy: str) -> None:
        self._model = model
        self.taxonomy = taxonomy

    def get_by_code(self, code: str) -> TaxonomyT:
        """Resolve a slug to its row, including retired ones.

        Inactive rows resolve on purpose: a project created last quarter still points at the
        engagement type that has since been deactivated, and refusing to read it would break the
        history rather than the picker.

        Args:
            code: Stable ASCII slug, e.g. ``"proyecto"``.

        Returns:
            The matching row.

        Raises:
            UnknownCode: No row in this taxonomy carries that code.
        """
        row = self.find_by_code(code)
        if row is None:
            raise UnknownCode(self.taxonomy, code)
        return row

    def find_by_code(self, code: str) -> TaxonomyT | None:
        """Resolve a slug, or ``None`` when it does not exist.

        For the callers that are deciding whether a code is valid — a fixture check, an admin
        form — where absence is an answer rather than a failure. Anything that needs the row in
        order to keep working uses :meth:`get_by_code` instead.

        Args:
            code: Stable ASCII slug.

        Returns:
            The matching row, or ``None``.
        """
        return self._model._default_manager.filter(code=code).first()

    def active_in_order(self) -> list[TaxonomyT]:
        """List the rows an operator may pick today, in display order.

        Filtering on ``is_active`` and sorting by ``(order, code)`` is served by the
        ``(is_active, order)`` index. ``code`` breaks ties so two rows sharing an ``order`` do not
        swap places between requests, which would look like the list is flickering.

        Returns:
            Active rows, ordered.
        """
        manager = self._model._default_manager
        return list(manager.filter(is_active=True).order_by("order", "code"))

    def all_in_order(self) -> list[TaxonomyT]:
        """List every row, active or retired, in display order.

        For the admin and for fixture verification, which must see retired rows; user-facing
        pickers use :meth:`active_in_order`.

        Returns:
            All rows, ordered.
        """
        return list(self._model._default_manager.order_by("order", "code"))

    def active_by_code(self) -> dict[str, TaxonomyT]:
        """Index the active rows by their slug.

        The shape a batch job wants: resolving 82 tasks against four priorities is one query plus
        dictionary lookups instead of 82 round trips.

        Returns:
            Mapping of ``code`` to row, covering the active rows only.
        """
        return {row.code: row for row in self.active_in_order()}


class PriorityRepository(TaxonomyRepository[Priority]):
    """Reads the priority taxonomy, plus the one question only it is asked."""

    def urgent_codes(self) -> frozenset[str]:
        """Codes the ``criticality`` signal counts as urgent.

        Read from ``is_urgent`` rather than from a literal set of codes, so adding or renaming a
        priority is a fixture row and never a Python change (``DATA_MODEL`` §12). Retired
        priorities are excluded: they cannot be assigned to new work, and an urgent count is a
        statement about what is open now.

        Returns:
            The active urgent codes, empty when the operation has marked none.
        """
        return frozenset(
            Priority.objects.filter(is_active=True, is_urgent=True).values_list("code", flat=True)
        )


#: The five taxonomies, as the singletons other contexts import.
engagement_types = TaxonomyRepository(EngagementType, taxonomy="engagement type")
project_types = TaxonomyRepository(ProjectType, taxonomy="project type")
stages = TaxonomyRepository(Stage, taxonomy="stage")
priorities = PriorityRepository(Priority, taxonomy="priority")
roles = TaxonomyRepository(Role, taxonomy="role")
