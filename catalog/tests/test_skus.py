"""SKUs and minimum stock levels (F06, F07)."""

from datetime import date
from decimal import Decimal

from django.db import IntegrityError, transaction
from django.test import TestCase

from catalog.models import Garment, MinimumStockLevel, Size, Sku, Warehouse
from catalog.services import PriceNotSet, next_sku_number, price_for_sku

from .factories import SEASON_START, make_garment, make_price


class SkuNumberTests(TestCase):
    """AsOne's control number: system assigned, unique, never reused."""

    def setUp(self):
        self.shirt = make_garment()
        self.size_10 = Size.objects.create(name="10", sort_order=10)
        self.size_12 = Size.objects.create(name="12", sort_order=12)

    def test_a_number_is_assigned_automatically(self):
        sku = Sku.objects.create(garment=self.shirt, size=self.size_10)

        self.assertTrue(sku.number)
        self.assertEqual(len(sku.number), 6, "AsOne's example is a six digit number")

    def test_numbers_do_not_repeat(self):
        first = Sku.objects.create(garment=self.shirt, size=self.size_10)
        second = Sku.objects.create(garment=self.shirt, size=self.size_12)

        self.assertNotEqual(first.number, second.number)

    def test_a_number_is_never_reused_after_a_deletion(self):
        """A sequence never goes backwards. A number means one product forever."""
        sku = Sku.objects.create(garment=self.shirt, size=self.size_10)
        retired = sku.number
        sku.delete()

        replacement = Sku.objects.create(garment=self.shirt, size=self.size_10)
        self.assertNotEqual(replacement.number, retired)

    def test_an_existing_number_never_changes_on_save(self):
        """It is printed on pick lists — it must mean the same thing forever."""
        sku = Sku.objects.create(garment=self.shirt, size=self.size_10)
        original = sku.number

        sku.is_active = False
        sku.save()

        sku.refresh_from_db()
        self.assertEqual(sku.number, original)

    def test_numbers_are_drawn_in_sequence(self):
        numbers = [int(next_sku_number()) for _ in range(3)]

        self.assertEqual(numbers, sorted(numbers))
        self.assertEqual(len(set(numbers)), 3)


class SkuIdentityTests(TestCase):
    def setUp(self):
        self.shirt = make_garment("White Shirt", Garment.SchoolLevel.PRIMARY, colour="White")
        self.size_10 = Size.objects.create(name="10", sort_order=10)

    def test_a_garment_and_size_pair_is_unique(self):
        """Two rows for the same product would split its stock in two."""
        Sku.objects.create(garment=self.shirt, size=self.size_10)

        with self.assertRaises(IntegrityError), transaction.atomic():
            Sku.objects.create(garment=self.shirt, size=self.size_10)

    def test_the_description_is_built_from_the_garment_and_size(self):
        sku = Sku.objects.create(garment=self.shirt, size=self.size_10)

        self.assertEqual(sku.description, "White Shirt White size 10 (PS)")

    def test_a_supplied_description_is_kept(self):
        sku = Sku.objects.create(
            garment=self.shirt, size=self.size_10, description="Custom label"
        )
        self.assertEqual(sku.description, "Custom label")

    def test_skus_sort_in_description_order(self):
        """Pick lists print "in Description sequence" (p.2)."""
        size_8 = Size.objects.create(name="8", sort_order=8)
        Sku.objects.create(garment=self.shirt, size=self.size_10)
        Sku.objects.create(garment=self.shirt, size=size_8)

        descriptions = list(Sku.objects.values_list("description", flat=True))
        self.assertEqual(descriptions, sorted(descriptions))


class SkuPricingTests(TestCase):
    """Price does not vary by size — every size reads through to the garment."""

    def setUp(self):
        self.shirt = make_garment()
        self.sizes = [
            Size.objects.create(name=str(n), sort_order=n) for n in (8, 10, 12, 14)
        ]
        self.skus = [
            Sku.objects.create(garment=self.shirt, size=size) for size in self.sizes
        ]

    def test_every_size_of_a_garment_costs_the_same(self):
        make_price(self.shirt, "25000.00", SEASON_START)

        prices = {price_for_sku(sku, SEASON_START) for sku in self.skus}
        self.assertEqual(prices, {Decimal("25000.00")})

    def test_repricing_the_garment_moves_every_size_at_once(self):
        make_price(self.shirt, "25000.00", SEASON_START)
        from catalog.services import reprice

        reprice(self.shirt, Decimal("30000.00"), date(2027, 6, 1))

        for sku in self.skus:
            self.assertEqual(price_for_sku(sku, date(2027, 7, 1)), Decimal("30000.00"))

    def test_an_unpriced_garment_leaves_its_skus_unpriced(self):
        with self.assertRaises(PriceNotSet):
            price_for_sku(self.skus[0], SEASON_START)


class MinimumStockLevelTests(TestCase):
    """F07 — the level that triggers a replenishment order, per warehouse."""

    def setUp(self):
        self.shirt = make_garment()
        self.size_10 = Size.objects.create(name="10", sort_order=10)
        self.sku = Sku.objects.create(garment=self.shirt, size=self.size_10)
        self.namayemba = Warehouse.objects.create(name="Namayemba")
        self.serere = Warehouse.objects.create(name="Serere")

    def test_a_sku_can_have_a_different_floor_at_each_warehouse(self):
        """The two warehouses serve different numbers of schools."""
        MinimumStockLevel.objects.create(
            sku=self.sku, warehouse=self.namayemba, minimum_quantity=100
        )
        MinimumStockLevel.objects.create(
            sku=self.sku, warehouse=self.serere, minimum_quantity=40
        )

        self.assertEqual(self.sku.minimum_levels.count(), 2)

    def test_only_one_floor_per_sku_per_warehouse(self):
        MinimumStockLevel.objects.create(
            sku=self.sku, warehouse=self.namayemba, minimum_quantity=100
        )

        with self.assertRaises(IntegrityError), transaction.atomic():
            MinimumStockLevel.objects.create(
                sku=self.sku, warehouse=self.namayemba, minimum_quantity=50
            )

    def test_a_floor_of_zero_is_allowed(self):
        """A SKU that should never be reordered automatically."""
        level = MinimumStockLevel.objects.create(
            sku=self.sku, warehouse=self.namayemba, minimum_quantity=0
        )
        self.assertEqual(level.minimum_quantity, 0)
