"""Releasing a paid order — F35, and the seam for open question Q2.

AsOne's chart shows an order waiting on Hold until "School Monitor"
confirms the invoice is paid. Nobody has told us what School Monitor is.

The decision this file records: that question changes **who calls
release_order()**, not what releasing means. So the transition is built and
tested, and the unknown lives in one permission class. These tests are what
will tell whoever answers Q2 whether their change broke anything.
"""

from datetime import date
from decimal import Decimal
from unittest import mock

from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from accounts.models import User
from accounts.tests.factories import build_sites, make_user
from catalog.models import Garment, GarmentPrice, Size, Sku
from inventory.models import MovementType
from inventory.services import post_movement
from orders.models.school_orders import OrderStatus
from orders.services import (
    CannotCancel,
    CannotRelease,
    cancel_order,
    pick_order,
    place_order,
    release_order,
)

IN_FORCE = date(2026, 1, 1)
ORDERED_ON = date(2026, 11, 10)
Role = User.Role


class ReleaseSetup(APITestCase):
    def setUp(self):
        self.sites = build_sites()
        self.school = self.sites["school_a"]
        self.warehouse = self.sites["namayemba"]

        self.clerk = make_user("chrisis", Role.SCHOOL_STAFF, school=self.school)
        self.finance = make_user("musana", Role.FINANCE)
        self.lead = make_user("sharon", Role.PROGRAM_LEAD)
        self.warehouse_staff = make_user(
            "julius", Role.WAREHOUSE_STAFF, warehouse=self.warehouse
        )

        garment = Garment.objects.create(name="White Shirt")
        GarmentPrice.objects.create(
            garment=garment, unit_price=Decimal("25000.00"), active_date=IN_FORCE
        )
        self.sku = Sku.objects.create(
            garment=garment, size=Size.objects.create(name="10", sort_order=10)
        )

    def an_order(self):
        return place_order(
            school=self.school,
            student_name="Miriam Achieng",
            order_date=ORDERED_ON,
            skus=[{"sku": self.sku, "quantity": 2}],
            created_by=self.clerk,
        )

    def stock(self, quantity):
        post_movement(
            warehouse=self.warehouse,
            sku=self.sku,
            quantity=quantity,
            movement_type=MovementType.RECEIPT,
            unit_value=Decimal("25000.00"),
            document_number="RC-100001",
            occurred_on=ORDERED_ON,
            created_by=self.warehouse_staff,
        )


class ReleasingAnOrder(ReleaseSetup):
    def test_a_released_order_records_who_and_when(self):
        order = release_order(self.an_order(), released_by=self.finance)

        self.assertEqual(order.status, OrderStatus.RELEASED)
        self.assertIsNotNone(order.released_at)
        self.assertEqual(order.released_by, self.finance)

    def test_the_payment_reference_is_kept(self):
        order = release_order(
            self.an_order(), released_by=self.finance, payment_reference="MTN-889123"
        )

        self.assertEqual(order.payment_reference, "MTN-889123")

    def test_a_reference_is_optional(self):
        """Until Q2 is answered we do not know what the real one looks like,
        so an empty one must not block the transition."""
        order = release_order(self.an_order(), released_by=self.finance)

        self.assertEqual(order.payment_reference, "")

    def test_a_cancelled_order_cannot_be_released(self):
        """It would resurrect a document the school withdrew."""
        order = cancel_order(self.an_order(), cancelled_by=self.clerk, reason="No funds.")

        with self.assertRaises(CannotRelease):
            release_order(order, released_by=self.finance)

    def test_an_order_cannot_be_released_twice(self):
        """The second release would overwrite who confirmed payment."""
        order = release_order(self.an_order(), released_by=self.finance)

        with self.assertRaises(CannotRelease):
            release_order(order, released_by=self.finance)

    def test_a_picked_order_cannot_be_released(self):
        self.stock(10)
        order = pick_order(self.an_order(), picked_by=self.warehouse_staff)

        with self.assertRaises(CannotRelease):
            release_order(order, released_by=self.finance)


class ReleasingClosesTheDoorOnCancelling(ReleaseSetup):
    """F36 says a school may withdraw an **unpaid** invoice. Once it is paid,
    what happens to the money is a question AsOne has not answered."""

    def test_a_released_order_can_no_longer_be_cancelled(self):
        order = release_order(self.an_order(), released_by=self.finance)

        with self.assertRaises(CannotCancel):
            cancel_order(order, cancelled_by=self.clerk, reason="Changed their mind.")


class PickingAndTheReleaseGate(ReleaseSetup):
    """`REQUIRE_RELEASE_BEFORE_PICK` is a placeholder for Q2, not a decision.

    Both branches are proven here so that whoever flips it can see exactly
    what changes, and so the current behaviour is a recorded choice rather
    than an accident.
    """

    def test_by_default_an_unpaid_order_can_still_be_picked(self):
        """Today's behaviour, and it is wrong on AsOne's chart — a warehouse
        should not pick an invoice nobody has paid. It stands because
        nothing could reach RELEASED when F39 was written."""
        self.stock(10)

        order = pick_order(self.an_order(), picked_by=self.warehouse_staff)
        self.assertEqual(order.status, OrderStatus.PICKED)

    @mock.patch("orders.services.fulfilment.REQUIRE_RELEASE_BEFORE_PICK", True)
    def test_with_the_gate_on_an_unpaid_order_is_refused(self):
        from orders.services import OrderCannotBePicked

        self.stock(10)

        with self.assertRaises(OrderCannotBePicked):
            pick_order(self.an_order(), picked_by=self.warehouse_staff)

    @mock.patch("orders.services.fulfilment.REQUIRE_RELEASE_BEFORE_PICK", True)
    def test_with_the_gate_on_a_released_order_still_picks(self):
        self.stock(10)
        order = release_order(self.an_order(), released_by=self.finance)

        picked = pick_order(order, picked_by=self.warehouse_staff)
        self.assertEqual(picked.status, OrderStatus.PICKED)


class WhoMayRelease(ReleaseSetup):
    """The Q2 seam, over HTTP. If AsOne answers "the school confirms it",
    this class is the file that changes."""

    def url(self, order):
        return reverse("orders:school-order-release", args=[order.pk])

    def test_finance_may_release(self):
        order = self.an_order()
        self.client.force_authenticate(self.finance)

        response = self.client.post(self.url(order), {"payment_reference": "RC-77"}, format="json")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["status"], OrderStatus.RELEASED)

    def test_a_school_clerk_may_not_confirm_their_own_payment(self):
        """Deliberate, and the thing to raise with AsOne before changing:
        the party who owes the money would be confirming it arrived."""
        order = self.an_order()
        self.client.force_authenticate(self.clerk)

        self.assertEqual(
            self.client.post(self.url(order), {}, format="json").status_code,
            status.HTTP_403_FORBIDDEN,
        )

    def test_the_warehouse_may_not(self):
        order = self.an_order()
        self.client.force_authenticate(self.warehouse_staff)

        self.assertEqual(
            self.client.post(self.url(order), {}, format="json").status_code,
            status.HTTP_403_FORBIDDEN,
        )

    def test_a_lead_may_not(self):
        order = self.an_order()
        self.client.force_authenticate(self.lead)

        self.assertEqual(
            self.client.post(self.url(order), {}, format="json").status_code,
            status.HTTP_403_FORBIDDEN,
        )

    def test_releasing_a_cancelled_order_is_a_400_not_a_500(self):
        order = cancel_order(self.an_order(), cancelled_by=self.clerk, reason="No funds.")
        self.client.force_authenticate(self.finance)

        response = self.client.post(self.url(order), {}, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
