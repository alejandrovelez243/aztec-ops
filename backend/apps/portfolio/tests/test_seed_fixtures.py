"""The committed seed fixtures still load, and load into the shape the models now declare.

``loaddata`` bypasses ``Model.save()`` and every form, so a fixture that has drifted from the schema
is only discovered when somebody runs ``make seed`` — usually on a fresh machine, usually while
demonstrating the product. This is the cheapest possible guard against that: it loads the same
seven files in the same order ``manage.py seed`` does and asserts the one relation whose *shape*
changed, ``Project.currency``, which was a three-letter string and is now a taxonomy row.

``TestCase``: the fixtures are ordinary rows, nothing here touches the outbox or ``on_commit``.
"""

from django.core.management import call_command
from django.test import TestCase

from apps.catalog.models import Currency
from apps.portfolio.management.commands.seed import FIXTURES
from apps.portfolio.models import Project


class SeedFixturesTestCase(TestCase):
    """What ``make seed`` must still be able to do to an empty database."""

    @classmethod
    def setUpTestData(cls) -> None:
        call_command("loaddata", *FIXTURES, verbosity=0)

    def test_every_seeded_project_points_at_a_currency_row(self) -> None:
        self.assertFalse(Project.objects.filter(currency__isnull=True).exists())

    def test_the_seeded_currencies_carry_the_decimals_the_frontend_formats_with(self) -> None:
        minor_units = dict(Currency.objects.values_list("code", "minor_units"))

        self.assertEqual(minor_units["USD"], 2)
        self.assertEqual(minor_units["CLP"], 0)

    def test_the_dataset_bills_in_the_two_currencies_it_was_imported_with(self) -> None:
        billed_in = set(Project.objects.values_list("currency__code", flat=True))

        self.assertEqual(billed_in, {"USD", "COP"})
