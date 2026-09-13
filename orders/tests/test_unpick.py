"""Undoing a pick — the safety net under F39.

Picking is one click and it reserves stock. Without a way back, a wrong
click refuses the next school's order for a shortfall that is not real, and
the wrong order joins the despatch queue and goes out on a van.

The tests that matter are about the ledger: that the stock genuinely becomes
free again, that nothing is deleted, and that the door closes the moment the
goods leave the building.
"""

from datetime import date
from decimal import Decimal

from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from accounts.models import User
from accounts.tests.factories import build_sites, make_user
from catalog.models import Garment, GarmentPrice, Size, Sku
from inventory.models import MovementType, StockMovement, StockStatus
from inventory.services import post_movement
from orders.models.school_orders import OrderStatus
from orders.services import (
    OrderCannotBeUnpicked,
    check_availability,
    pick_order,
    place_order,
    release_order,
    ship_order,
    unpick_order,
)

IN_FORCE = date(2026, 1, 1)
TODAY = date(2026, 11, 10)
Role = User.Role


class UnpickSetup(APITestCase):
    def setUp(self):
        self.sites = build_sites()
        self.namayemba = self.sites["namayemba"]
        self.school = self.sites["school_a"]

        self.julius = make_user("julius", Role.WAREHOUSE_STAFF, warehouse=self.namayemba)
        self.finance = make_user("musana", Role.FINANCE)
        self.clerk = make_user("chrisis", Role.SCHOOL_STAFF, school=self.school)

        garment = Garment.objects.create(name="White Shirt")
        GarmentPrice.objects.create(
            garment=garment, unit_price=Decimal("25000.00"), active_date=IN_FORCE
        )
        self.shirt = Sku.objects.create(
            garment=garment, size=Size.objects.create(name="S8", sort_order=10)
        )

    def stock(self, quantity):
        post_movement(
            warehouse=self.namayemba,
            sku=self.shirt,
            quantity=quantity,
            movement_type=MovementType.RECEIPT,
            unit_value=Decimal("25000.00"),
            document_number="RC-100001",
            occurred_on=IN_FORCE,
            created_by=self.julius,
        )

    def order_for(self, student, quantity=2):
        order = place_order(
            school=self.school,
            student_name=student,
            order_date=TODAY,
            skus=[{"sku": self.shirt, "quantity": quantity}],
            created_by=self.clerk,
        )
        release_order(order, released_by=self.finance)
        order.refresh_from_db()
        return order

    def available(self):
        rows = StockMovement.objects.filter(
            warehouse=self.namayemba,
            sku=self.shirt,
            stock_status=StockStatus.AVAILABLE,
        )
        return sum(row.quantity for row in rows)

    def reserved(self):
        rows = StockMovement.objects.filter(
            warehouse=self.namayemba, sku=self.shirt, stock_status=StockStatus.PICK
        )
        return sum(row.quantity for row in rows)


class PuttingItBack(UnpickSetup):
    def test_the_stock_becomes_free_again(self):
        self.stock(10)
        order = self.order_for("Nakato Grace")
        pick_order(order, picked_by=self.julius)

        self.assertEqual(self.available(), 8)
        self.assertEqual(self.reserved(), 2)

        unpick_order(order, unpicked_by=self.julius)

        self.assertEqual(self.available(), 10)
        self.assertEqual(self.reserved(), 0)

    def test_the_order_goes_back_to_waiting_to_be_picked(self):
        self.stock(10)
        order = self.order_for("Nakato Grace")
        pick_order(order, picked_by=self.julius)

        unpick_order(order, unpicked_by=self.julius)
        order.refresh_from_db()

        self.assertEqual(order.status, OrderStatus.RELEASED)

    def test_nothing_is_deleted_from_the_ledger(self):
        """Append-only: the undo is an offsetting entry, not an erasure."""
        self.stock(10)
        order = self.order_for("Nakato Grace")
        pick_order(order, picked_by=self.julius)

        before = StockMovement.objects.count()
        unpick_order(order, unpicked_by=self.julius)

        self.assertEqual(StockMovement.objects.count(), before + 2)

    def test_the_undo_records_who_did_it(self):
        self.stock(10)
        order = self.order_for("Nakato Grace")
        pick_order(order, picked_by=self.julius)

        joan = make_user("joan", Role.WAREHOUSE_STAFF, warehouse=self.namayemba)
        unpick_order(order, unpicked_by=joan, reason="Wrong row")

        newest = StockMovement.objects.latest("id")
        self.assertEqual(newest.created_by, joan)

    def test_the_reason_is_kept_on_the_order(self):
        self.stock(10)
        order = self.order_for("Nakato Grace")
        pick_order(order, picked_by=self.julius)

        unpick_order(order, unpicked_by=self.julius, reason="Picked the wrong row")
        order.refresh_from_db()

        self.assertIn("Picked the wrong row", order.notes)


class ItUnblocksTheNextOrder(UnpickSetup):
    """The reason this exists: a wrong pick refuses a real order."""

    def test_stock_freed_by_an_undo_can_fill_another_order(self):
        self.stock(2)
        wrong = self.order_for("Wrong Child")
        pick_order(wrong, picked_by=self.julius)

        # The right order now looks short, though the stock is on the shelf.
        right = self.order_for("Right Child")
        self.assertTrue(any(row["shortfall"] > 0 for row in check_availability(right)))

        unpick_order(wrong, unpicked_by=self.julius)

        self.assertTrue(all(row["shortfall"] == 0 for row in check_availability(right)))


class TheDoorClosesWhenTheGoodsLeave(UnpickSetup):
    def test_a_shipped_order_cannot_be_unpicked(self):
        self.stock(10)
        order = self.order_for("Nakato Grace")
        pick_order(order, picked_by=self.julius)
        ship_order(order, shipped_by=self.julius, shipped_on=TODAY)
        order.refresh_from_db()

        with self.assertRaises(OrderCannotBeUnpicked):
            unpick_order(order, unpicked_by=self.julius)

    def test_an_order_that_was_never_picked_has_nothing_to_put_back(self):
        self.stock(10)
        order = self.order_for("Nakato Grace")

        with self.assertRaises(OrderCannotBeUnpicked):
            unpick_order(order, unpicked_by=self.julius)


class UnpickingOverHttp(UnpickSetup):
    def url(self, order):
        return reverse("orders:school-order-unpick", args=[order.pk])

    def test_a_warehouse_clerk_may_undo_a_pick(self):
        self.stock(10)
        order = self.order_for("Nakato Grace")
        pick_order(order, picked_by=self.julius)
        self.client.force_authenticate(self.julius)

        response = self.client.post(self.url(order), {"reason": "Wrong row"}, format="json")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["status"], OrderStatus.RELEASED)

    def test_a_school_clerk_may_not(self):
        """Putting stock back is a warehouse act."""
        self.stock(10)
        order = self.order_for("Nakato Grace")
        pick_order(order, picked_by=self.julius)
        self.client.force_authenticate(self.clerk)

        self.assertEqual(
            self.client.post(self.url(order), {}, format="json").status_code,
            status.HTTP_403_FORBIDDEN,
        )

    def test_undoing_a_shipped_order_is_a_400_that_explains(self):
        self.stock(10)
        order = self.order_for("Nakato Grace")
        pick_order(order, picked_by=self.julius)
        ship_order(order, shipped_by=self.julius, shipped_on=TODAY)
        self.client.force_authenticate(self.julius)

        response = self.client.post(self.url(order), {}, format="json")

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("return", str(response.data).lower())
