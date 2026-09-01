"""Physical count correction (F24) — the system does the comparing.

Bashir, 30 August 2026: the checklist requirement is "compare counted
quantities to system figures and post the difference." F23's generic
adjustment endpoint (and the CORR_UP/CORR_DOWN split it already tested in
test_correction_codes.py) makes the *person* do that comparison — this file
tests the thing that actually does it: services.correct_count() and the
/adjustments/correct-count/ endpoint built on top of it.
"""

from datetime import date
from decimal import Decimal

from django.test import TestCase
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from accounts.models import User
from accounts.tests.factories import build_sites, make_user
from catalog.models import Garment, GarmentPrice, Size, Sku
from catalog.services import PriceNotSet
from inventory.models import InventoryAdjustment, MovementType, ReasonCode
from inventory.services import correct_count, post_movement, stock_level

Role = User.Role
COUNTED_ON = date(2026, 11, 1)


class CountCorrectionSetup(TestCase):
    def setUp(self):
        self.sites = build_sites()
        self.warehouse = self.sites["namayemba"]
        self.finance = make_user("musana", Role.FINANCE)

        garment = Garment.objects.create(name="White Shirt")
        GarmentPrice.objects.create(
            garment=garment, unit_price=Decimal("25000.00"), active_date=date(2026, 1, 1)
        )
        self.sku = Sku.objects.create(
            garment=garment, size=Size.objects.create(name="10", sort_order=10)
        )

        # Same codes migration 0006 creates — not stand-ins, so these tests
        # match what actually exists after the split.
        ReasonCode.objects.create(
            code="CORR_UP",
            name="Inventory correction — count higher",
            direction=ReasonCode.AdjustmentDirection.INCREASE,
        )
        ReasonCode.objects.create(
            code="CORR_DOWN",
            name="Inventory correction — count lower",
            direction=ReasonCode.AdjustmentDirection.DECREASE,
        )

    def stock_in(self, quantity, on=date(2026, 10, 1)):
        return post_movement(
            warehouse=self.warehouse,
            sku=self.sku,
            quantity=quantity,
            movement_type=MovementType.RECEIPT,
            unit_value=Decimal("25000.00"),
            document_number="RC-SETUP",
            occurred_on=on,
            created_by=self.finance,
        )


class TheSystemDoesTheComparing(CountCorrectionSetup):
    """The whole point of F24: the caller supplies only what was counted."""

    def test_a_higher_count_posts_corr_up_for_the_difference(self):
        self.stock_in(500)

        adjustment = correct_count(
            warehouse=self.warehouse,
            sku=self.sku,
            counted_quantity=520,
            adjustment_date=COUNTED_ON,
            created_by=self.finance,
        )

        self.assertEqual(adjustment.reason_code.code, "CORR_UP")
        self.assertEqual(adjustment.quantity, 20)
        self.assertEqual(stock_level(self.sku, self.warehouse), 520)

    def test_a_lower_count_posts_corr_down_for_the_difference(self):
        self.stock_in(500)

        adjustment = correct_count(
            warehouse=self.warehouse,
            sku=self.sku,
            counted_quantity=480,
            adjustment_date=COUNTED_ON,
            created_by=self.finance,
        )

        self.assertEqual(adjustment.reason_code.code, "CORR_DOWN")
        self.assertEqual(adjustment.quantity, 20)
        self.assertEqual(stock_level(self.sku, self.warehouse), 480)

    def test_a_count_that_matches_the_system_posts_nothing(self):
        """A match is the ordinary outcome of most counts, not an error."""
        self.stock_in(500)

        result = correct_count(
            warehouse=self.warehouse,
            sku=self.sku,
            counted_quantity=500,
            adjustment_date=COUNTED_ON,
            created_by=self.finance,
        )

        self.assertIsNone(result)
        self.assertEqual(InventoryAdjustment.objects.count(), 0)
        self.assertEqual(stock_level(self.sku, self.warehouse), 500)

    def test_counting_stock_where_the_system_has_none_posts_corr_up(self):
        adjustment = correct_count(
            warehouse=self.warehouse,
            sku=self.sku,
            counted_quantity=15,
            adjustment_date=COUNTED_ON,
            created_by=self.finance,
        )

        self.assertEqual(adjustment.reason_code.code, "CORR_UP")
        self.assertEqual(adjustment.quantity, 15)

    def test_counting_zero_against_full_stock_posts_corr_down_for_all_of_it(self):
        """The boundary case: a count of zero must not be refused as if it
        were decreasing more than exists — it is exactly what exists."""
        self.stock_in(30)

        adjustment = correct_count(
            warehouse=self.warehouse,
            sku=self.sku,
            counted_quantity=0,
            adjustment_date=COUNTED_ON,
            created_by=self.finance,
        )

        self.assertEqual(adjustment.reason_code.code, "CORR_DOWN")
        self.assertEqual(adjustment.quantity, 30)
        self.assertEqual(stock_level(self.sku, self.warehouse), 0)


class ACorrectionPostsImmediately(CountCorrectionSetup):
    """Unlike the rest of F23, this is not a two-step prepare-then-post —
    see correct_count()'s docstring for why."""

    def test_the_returned_adjustment_is_already_posted(self):
        self.stock_in(500)

        adjustment = correct_count(
            warehouse=self.warehouse,
            sku=self.sku,
            counted_quantity=510,
            adjustment_date=COUNTED_ON,
            created_by=self.finance,
        )

        self.assertTrue(adjustment.is_posted)
        self.assertIsNotNone(adjustment.unit_value)

    def test_it_is_valued_at_the_catalog_price(self):
        self.stock_in(500)

        adjustment = correct_count(
            warehouse=self.warehouse,
            sku=self.sku,
            counted_quantity=510,
            adjustment_date=COUNTED_ON,
            created_by=self.finance,
        )

        self.assertEqual(adjustment.unit_value, Decimal("25000.00"))


class CorrectingAnUnpricedSku(CountCorrectionSetup):
    def test_raises_rather_than_posting_at_zero(self):
        unpriced = Garment.objects.create(name="Unpriced Blazer")
        unpriced_sku = Sku.objects.create(
            garment=unpriced, size=Size.objects.create(name="12", sort_order=12)
        )

        with self.assertRaises(PriceNotSet):
            correct_count(
                warehouse=self.warehouse,
                sku=unpriced_sku,
                counted_quantity=5,
                adjustment_date=COUNTED_ON,
                created_by=self.finance,
            )


class CountCorrectionApi(APITestCase):
    """Finance only, same as the rest of F23 — see InventoryAdjustmentApi
    for the exhaustive per-role check; this confirms the new action inherits
    it rather than re-asserting the whole matrix."""

    def setUp(self):
        self.sites = build_sites()
        self.warehouse = self.sites["namayemba"]
        self.finance = make_user("musana", Role.FINANCE)
        self.lead = make_user("sharon", Role.PROGRAM_LEAD)

        garment = Garment.objects.create(name="White Shirt")
        GarmentPrice.objects.create(
            garment=garment, unit_price=Decimal("25000.00"), active_date=date(2026, 1, 1)
        )
        self.sku = Sku.objects.create(
            garment=garment, size=Size.objects.create(name="10", sort_order=10)
        )
        ReasonCode.objects.create(
            code="CORR_UP",
            name="Inventory correction — count higher",
            direction=ReasonCode.AdjustmentDirection.INCREASE,
        )
        ReasonCode.objects.create(
            code="CORR_DOWN",
            name="Inventory correction — count lower",
            direction=ReasonCode.AdjustmentDirection.DECREASE,
        )
        post_movement(
            warehouse=self.warehouse,
            sku=self.sku,
            quantity=500,
            movement_type=MovementType.RECEIPT,
            unit_value=Decimal("25000.00"),
            document_number="RC-SETUP",
            occurred_on=date(2026, 10, 1),
            created_by=self.finance,
        )

    def correct(self, counted_quantity):
        return self.client.post(
            reverse("inventory:adjustment-correct-count"),
            {
                "warehouse": self.warehouse.pk,
                "sku": self.sku.pk,
                "counted_quantity": counted_quantity,
                "adjustment_date": COUNTED_ON.isoformat(),
            },
            format="json",
        )

    def test_a_discrepancy_posts_and_returns_201(self):
        self.client.force_authenticate(self.finance)
        response = self.correct(520)

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data["reason_code_code"], "CORR_UP")
        self.assertTrue(response.data["is_posted"])

    def test_a_match_returns_200_with_no_adjustment(self):
        self.client.force_authenticate(self.finance)
        response = self.correct(500)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIsNone(response.data["adjustment"])

    def test_stock_actually_changes_over_the_api(self):
        self.client.force_authenticate(self.finance)
        self.correct(480)

        self.assertEqual(stock_level(self.sku, self.warehouse), 480)

    def test_nobody_but_finance_may_use_it(self):
        """Same Finance-only column as the rest of F23 — including leads."""
        self.client.force_authenticate(self.lead)
        response = self.correct(520)

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
