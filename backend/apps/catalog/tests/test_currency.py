"""What the currency taxonomy guarantees to the client that formats an amount with it.

Split by base class on purpose (CLAUDE.md rule 15): the reference shape is pure and is asserted on
``SimpleTestCase``, which *forbids* database access and therefore proves the value object needs
none; everything about rows, constraints and the served document is a ``TestCase``.
"""

from django.db.utils import DataError, IntegrityError
from django.test import SimpleTestCase, TestCase
from pydantic import ValidationError

from apps.accounts.tests.support import bearer, make_member
from apps.catalog.domain.value_objects import CurrencyRef
from apps.catalog.models import Currency, EngagementType, Priority
from apps.catalog.services import read_catalog


class CurrencyRefTestCase(SimpleTestCase):
    """The shape the frontend formats with, independent of any row."""

    def test_minor_units_outside_the_iso_range_is_refused_at_construction(self) -> None:
        with self.assertRaises(ValidationError):
            CurrencyRef(code="USD", label="Dolar", minor_units=9)

    def test_a_currency_is_still_a_taxonomy_reference(self) -> None:
        ref = CurrencyRef(code="CLP", label="Peso chileno", minor_units=0)

        self.assertEqual((ref.code, ref.label, ref.color), ("CLP", "Peso chileno", None))
        self.assertEqual(ref.minor_units, 0)


class CurrencyRowTestCase(TestCase):
    """The database's own refusals: a currency that would render an unreadable amount."""

    def test_a_code_outside_iso_4217_is_refused_by_the_database(self) -> None:
        with self.assertRaises((IntegrityError, DataError)):
            Currency.objects.create(code="usd", label="Dolar")

    def test_minor_units_beyond_the_standard_is_refused_by_the_database(self) -> None:
        with self.assertRaises((IntegrityError, DataError)):
            Currency.objects.create(code="XXX", label="Invented", minor_units=7)

    def test_the_row_publishes_the_decimal_places_a_client_cannot_guess(self) -> None:
        currency = Currency.objects.create(code="CLP", label="Peso chileno", minor_units=0)

        self.assertEqual(currency.to_currency_ref().minor_units, 0)


class CatalogDocumentTestCase(TestCase):
    """What ``GET /api/v1/catalog`` promises the frontend it will never have to hardcode."""

    auth: dict[str, str]

    @classmethod
    def setUpTestData(cls) -> None:
        # The catalog is a read, and reads are authenticated too: the vocabulary is not secret, but
        # a public endpoint is a public endpoint, and the exception list is five routes long.
        make_member(code="catalog.reader")
        cls.auth = bearer(username="catalog.reader")
        Currency.objects.create(code="USD", label="Dolar estadounidense", order=1)
        Currency.objects.create(code="CLP", label="Peso chileno", minor_units=0, order=2)
        Currency.objects.create(code="JPY", label="Yen", minor_units=0, order=3, is_active=False)
        EngagementType.objects.create(code="proyecto", label="Proyecto")
        Priority.objects.create(code="critica", label="Critica", is_urgent=True)

    def test_currencies_are_served_beside_every_other_taxonomy(self) -> None:
        response = self.client.get("/api/v1/catalog", **self.auth)

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual([row["code"] for row in body["priorities"]], ["critica"])
        self.assertEqual([row["code"] for row in body["currencies"]], ["USD", "CLP"])

    def test_every_served_currency_carries_its_minor_units(self) -> None:
        response = self.client.get("/api/v1/catalog", **self.auth)

        served = {row["code"]: row["minor_units"] for row in response.json()["currencies"]}
        self.assertEqual(served, {"USD": 2, "CLP": 0})

    def test_a_retired_currency_leaves_the_picker_without_being_deleted(self) -> None:
        served = read_catalog()

        self.assertNotIn("JPY", [row.code for row in served.currencies])
        self.assertIsNotNone(Currency.objects.find_by_code("JPY"))
