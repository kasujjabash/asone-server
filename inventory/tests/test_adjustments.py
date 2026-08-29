"""Inventory adjustments (F23) — a quantity change against a reason code.

The generic shape the rest of Phase 2 reuses. Three rules matter most:

    1. The reason code decides the sign, not the person posting it.
    2. Value is the SKU's catalog price, looked up fresh at posting time.
    3. Entering and posting are separate — nothing changes stock until
       post_adjustment() runs.
"""

from datetime import date, timedelta
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from accounts.models import User
from accounts.tests.factories import build_sites, make_user
from catalog.models import Garment, GarmentPrice, Size, Sku
from catalog.services import PriceNotSet, reprice
from inventory.models import InventoryAdjustment, MovementType, ReasonCode, StockMovement
from inventory.services import (
    AdjustmentAlreadyPosted,
    create_adjustment,
    post_adjustment,
    stock_level,
)

ADJUSTED_ON = date(2026, 11, 1)
Role = User.Role
Direction = ReasonCode.AdjustmentDirection


class AdjustmentSetup(TestCase):
    def setUp(self):
        self.sites = build_sites()
        self.namayemba = self.sites["namayemba"]

        self.finance = make_user("musana", Role.FINANCE)
        self.lead = make_user("sharon", Role.PROGRAM_LEAD)

        garment = Garment.objects.create(name="White Shirt")
        GarmentPrice.objects.create(
            garment=garment, unit_price=Decimal("25000.00"), active_date=date(2026, 1, 1)
        )
        self.sku = Sku.objects.create(
            garment=garment, size=Size.objects.create(name="10", sort_order=10)
        )

        self.damage = ReasonCode.objects.create(
            code="DMG", name="Damaged", direction=Direction.DECREASE
        )
        self.found = ReasonCode.objects.create(
            code="FOUND", name="Found in count", direction=Direction.INCREASE
        )

    def adjust(self, reason_code, quantity=10, **fields):
        return create_adjustment(
            warehouse=self.namayemba,
            sku=self.sku,
            quantity=quantity,
            reason_code=reason_code,
            adjustment_date=ADJUSTED_ON,
            created_by=self.finance,
            **fields,
        )


class PreparingAnAdjustmentDoesNotTouchStock(AdjustmentSetup):
    def test_creating_an_adjustment_leaves_stock_at_zero(self):
        self.adjust(self.damage)

        self.assertEqual(stock_level(self.sku, self.namayemba), 0)

    def test_the_number_is_assigned_automatically(self):
        adjustment = self.adjust(self.damage)

        self.assertTrue(adjustment.number.startswith("ADJ-"))

    def test_a_new_adjustment_is_not_posted(self):
        adjustment = self.adjust(self.damage)

        self.assertFalse(adjustment.is_posted)
        self.assertIsNone(adjustment.unit_value)

    def test_quantity_is_entered_as_a_plain_positive_count(self):
        """The sign lives on the reason code, not on what gets typed in."""
        adjustment = self.adjust(self.damage, quantity=7)

        self.assertEqual(adjustment.quantity, 7)

    def test_an_unpriced_sku_is_refused_up_front(self):
        """A gap that would only be found at posting time is refused before
        the adjustment is even written down."""
        unpriced = Garment.objects.create(name="Unpriced Blazer")
        unpriced_sku = Sku.objects.create(
            garment=unpriced, size=Size.objects.create(name="12", sort_order=12)
        )

        with self.assertRaises(PriceNotSet):
            create_adjustment(
                warehouse=self.namayemba,
                sku=unpriced_sku,
                quantity=5,
                reason_code=self.damage,
                adjustment_date=ADJUSTED_ON,
                created_by=self.finance,
            )


class ReasonCodeDecidesTheDirection(AdjustmentSetup):
    """The rule F23 exists to enforce: nobody posting an adjustment chooses
    the sign — the reason code already has one."""

    def test_a_decrease_code_reduces_stock(self):
        adjustment = self.adjust(self.damage, quantity=10)
        post_adjustment(adjustment, posted_by=self.finance)

        self.assertEqual(stock_level(self.sku, self.namayemba), -10)

    def test_an_increase_code_raises_stock(self):
        adjustment = self.adjust(self.found, quantity=10)
        post_adjustment(adjustment, posted_by=self.finance)

        self.assertEqual(stock_level(self.sku, self.namayemba), 10)

    def test_the_ledger_row_carries_the_signed_quantity(self):
        adjustment = self.adjust(self.damage, quantity=6)
        post_adjustment(adjustment, posted_by=self.finance)

        movement = StockMovement.objects.get(document_number=adjustment.number)
        self.assertEqual(movement.quantity, -6)


class PostingWritesOneLedgerRow(AdjustmentSetup):
    def test_posting_creates_exactly_one_movement(self):
        adjustment = self.adjust(self.damage)
        post_adjustment(adjustment, posted_by=self.finance)

        self.assertEqual(
            StockMovement.objects.filter(document_number=adjustment.number).count(), 1
        )

    def test_the_movement_type_is_adjustment(self):
        adjustment = self.adjust(self.damage)
        post_adjustment(adjustment, posted_by=self.finance)

        movement = StockMovement.objects.get(document_number=adjustment.number)
        self.assertEqual(movement.movement_type, MovementType.ADJUSTMENT)

    def test_source_and_destination_are_left_blank(self):
        """Bashir, 28 August 2026: leave them blank for now."""
        adjustment = self.adjust(self.damage)
        post_adjustment(adjustment, posted_by=self.finance)

        movement = StockMovement.objects.get(document_number=adjustment.number)
        self.assertEqual(movement.source, "")
        self.assertEqual(movement.destination, "")

    def test_posting_marks_the_adjustment_posted(self):
        adjustment = self.adjust(self.damage)
        post_adjustment(adjustment, posted_by=self.finance)

        adjustment.refresh_from_db()
        self.assertTrue(adjustment.is_posted)
        self.assertIsNotNone(adjustment.posted_at)

    def test_posting_twice_is_refused(self):
        adjustment = self.adjust(self.damage)
        post_adjustment(adjustment, posted_by=self.finance)

        with self.assertRaises(AdjustmentAlreadyPosted):
            post_adjustment(adjustment, posted_by=self.finance)

        self.assertEqual(
            StockMovement.objects.filter(document_number=adjustment.number).count(), 1
        )


class ValueComesFromTheCatalogPrice(AdjustmentSetup):
    """Bashir, 28 August 2026: use the SKU's catalog price, not the ledger's
    weighted average — an adjustment corrects a count, it does not carry
    forward a value from stock that already moved."""

    def test_the_movement_value_matches_the_catalog_price(self):
        adjustment = self.adjust(self.damage, quantity=4)
        post_adjustment(adjustment, posted_by=self.finance)

        movement = StockMovement.objects.get(document_number=adjustment.number)
        self.assertEqual(movement.unit_value, Decimal("25000.00"))

    def test_the_adjustment_itself_records_the_value_once_posted(self):
        adjustment = self.adjust(self.damage)
        post_adjustment(adjustment, posted_by=self.finance)

        adjustment.refresh_from_db()
        self.assertEqual(adjustment.unit_value, Decimal("25000.00"))

    def test_a_reprice_between_creating_and_posting_uses_the_posting_price(self):
        """The price is looked up again at posting, not reused from when the
        adjustment was written down — a reprice can happen in between."""
        adjustment = self.adjust(self.damage)

        reprice(self.sku.garment, Decimal("30000.00"), ADJUSTED_ON)
        post_adjustment(adjustment, posted_by=self.finance)

        movement = StockMovement.objects.get(document_number=adjustment.number)
        self.assertEqual(movement.unit_value, Decimal("30000.00"))

    def test_posting_is_refused_if_the_price_disappears_before_posting(self):
        """A gap missed at creation because the price existed then, but was
        removed before posting — the ledger must still refuse to guess."""
        adjustment = self.adjust(self.damage)

        # Close the only price with nothing to replace it.
        self.sku.garment.prices.update(expiration_date=ADJUSTED_ON)

        with self.assertRaises(PriceNotSet):
            post_adjustment(adjustment, posted_by=self.finance)


class InventoryAdjustmentModelRules(AdjustmentSetup):
    def test_quantity_must_be_positive(self):
        adjustment = InventoryAdjustment(
            warehouse=self.namayemba,
            sku=self.sku,
            quantity=0,
            reason_code=self.damage,
            adjustment_date=ADJUSTED_ON,
            created_by=self.finance,
        )
        with self.assertRaises(ValidationError):
            adjustment.full_clean(exclude=["number"])


class InventoryAdjustmentApi(APITestCase):
    """Finance only, for both reading and writing — see
    accounts.permissions.CanAdjustInventory. Unlike most master data, even
    the leads are excluded."""

    def setUp(self):
        self.sites = build_sites()
        self.namayemba = self.sites["namayemba"]

        self.users = {
            Role.PROGRAM_LEAD: make_user("sharon", Role.PROGRAM_LEAD),
            Role.OPERATIONS_MANAGER: make_user("andrew", Role.OPERATIONS_MANAGER),
            Role.FINANCE: make_user("musana", Role.FINANCE),
            Role.WAREHOUSE_STAFF: make_user(
                "julius", Role.WAREHOUSE_STAFF, warehouse=self.namayemba
            ),
            Role.SCHOOL_STAFF: make_user(
                "chrisis", Role.SCHOOL_STAFF, school=self.sites["school_a"]
            ),
        }

        garment = Garment.objects.create(name="White Shirt")
        GarmentPrice.objects.create(
            garment=garment, unit_price=Decimal("25000.00"), active_date=date(2026, 1, 1)
        )
        self.sku = Sku.objects.create(
            garment=garment, size=Size.objects.create(name="10", sort_order=10)
        )
        self.damage = ReasonCode.objects.create(
            code="DMG", name="Damaged", direction=Direction.DECREASE
        )

    def as_role(self, role):
        self.client.force_authenticate(self.users[role])

    def create_payload(self):
        return {
            "warehouse": self.namayemba.pk,
            "sku": self.sku.pk,
            "quantity": 5,
            "reason_code": self.damage.pk,
            "adjustment_date": ADJUSTED_ON.isoformat(),
        }

    def test_finance_can_create_an_adjustment(self):
        self.as_role(Role.FINANCE)

        response = self.client.post(
            reverse("inventory:adjustment-list"), self.create_payload(), format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertFalse(response.data["is_posted"])

    def test_nobody_else_can_create_an_adjustment(self):
        """Including the leads — the matrix gives this column to Finance alone."""
        for role in (
            Role.PROGRAM_LEAD,
            Role.OPERATIONS_MANAGER,
            Role.WAREHOUSE_STAFF,
            Role.SCHOOL_STAFF,
        ):
            with self.subTest(role=role):
                self.as_role(role)
                response = self.client.post(
                    reverse("inventory:adjustment-list"), self.create_payload(), format="json"
                )
                self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_nobody_else_can_even_read_adjustments(self):
        for role in (
            Role.PROGRAM_LEAD,
            Role.OPERATIONS_MANAGER,
            Role.WAREHOUSE_STAFF,
            Role.SCHOOL_STAFF,
        ):
            with self.subTest(role=role):
                self.as_role(role)
                response = self.client.get(reverse("inventory:adjustment-list"))
                self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_creating_an_adjustment_for_an_unpriced_sku_is_a_400_not_a_500(self):
        unpriced = Garment.objects.create(name="Unpriced Blazer")
        unpriced_sku = Sku.objects.create(
            garment=unpriced, size=Size.objects.create(name="12", sort_order=12)
        )
        self.as_role(Role.FINANCE)

        payload = self.create_payload()
        payload["sku"] = unpriced_sku.pk
        response = self.client.post(reverse("inventory:adjustment-list"), payload, format="json")

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("sku", response.data)

    def test_posting_through_the_api_changes_stock(self):
        self.as_role(Role.FINANCE)
        created = self.client.post(
            reverse("inventory:adjustment-list"), self.create_payload(), format="json"
        )

        response = self.client.post(
            reverse("inventory:adjustment-post-to-ledger", args=[created.data["id"]])
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data["is_posted"])
        self.assertEqual(stock_level(self.sku, self.namayemba), -5)

    def test_posting_twice_through_the_api_is_a_400_not_a_500(self):
        self.as_role(Role.FINANCE)
        created = self.client.post(
            reverse("inventory:adjustment-list"), self.create_payload(), format="json"
        )
        pk = created.data["id"]
        self.client.post(reverse("inventory:adjustment-post-to-ledger", args=[pk]))

        response = self.client.post(reverse("inventory:adjustment-post-to-ledger", args=[pk]))
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_an_adjustment_cannot_be_deleted(self):
        self.as_role(Role.FINANCE)
        created = self.client.post(
            reverse("inventory:adjustment-list"), self.create_payload(), format="json"
        )

        response = self.client.delete(
            reverse("inventory:adjustment-detail", args=[created.data["id"]])
        )
        self.assertEqual(response.status_code, status.HTTP_405_METHOD_NOT_ALLOWED)
