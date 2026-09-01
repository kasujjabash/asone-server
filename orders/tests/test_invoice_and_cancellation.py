"""The invoice, cancellation and the on-hold queue — F34, F36, F53.

Three features that between them cover an order's life up to the point where
AsOne has not yet answered how payment is confirmed:

    raised  ->  looked at  ->  withdrawn if nobody pays

The rules worth protecting:

    An invoice shows the kit the school chose, not a flat list of garments.
    An order is cancelled, never deleted — a parent is holding the number.
    Only an unpaid order on hold may be cancelled.
"""

from datetime import date
from decimal import Decimal

from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from accounts.models import User
from accounts.tests.factories import build_sites, make_user
from catalog.models import Garment, GarmentPrice, Kit, KitItem, Size, Sku
from orders.models import SchoolOrder
from orders.models.school_orders import OrderStatus
from orders.services import CannotCancel, cancel_order, place_order

IN_FORCE = date(2026, 1, 1)
ORDERED_ON = date(2026, 11, 10)
Role = User.Role
Level = Garment.SchoolLevel


class InvoiceSetup(APITestCase):
    def setUp(self):
        self.sites = build_sites()
        self.school = self.sites["school_a"]        # Namayemba PS, Primary
        self.other_school = self.sites["school_b"]  # Serere HS, High

        self.chrisis = make_user("chrisis", Role.SCHOOL_STAFF, school=self.school)
        self.peter = make_user("peter", Role.SCHOOL_STAFF, school=self.other_school)
        self.lead = make_user("sharon", Role.PROGRAM_LEAD)
        self.finance = make_user("musana", Role.FINANCE)
        self.clerk = make_user(
            "julius", Role.WAREHOUSE_STAFF, warehouse=self.sites["namayemba"]
        )

        self.shirt = self.priced_sku("White Shirt", Level.BOTH, "25000.00")
        self.tunic = self.priced_sku("Blue Tunic", Level.PRIMARY, "30000.00")
        self.socks = self.priced_sku("Socks", Level.BOTH, "5000.00")

        self.kit = Kit.objects.create(
            kit_number="PS-STARTER",
            name="PS Starter Kit",
            school_level=Kit.SchoolLevel.PRIMARY,
        )
        KitItem.objects.create(kit=self.kit, sku=self.shirt, quantity=2)
        KitItem.objects.create(kit=self.kit, sku=self.tunic, quantity=1)

    def priced_sku(self, name, level, price):
        garment = Garment.objects.create(name=name, school_level=level)
        GarmentPrice.objects.create(
            garment=garment, unit_price=Decimal(price), active_date=IN_FORCE
        )
        return Sku.objects.create(
            garment=garment, size=Size.objects.create(name=f"{name[:6]}-10", sort_order=10)
        )

    def an_order(self, school=None, with_kit=True, name="Miriam Achieng"):
        return place_order(
            school=school or self.school,
            student_name=name,
            order_date=ORDERED_ON,
            kits=[{"kit": self.kit, "quantity": 1}] if with_kit else [],
            skus=[{"sku": self.socks, "quantity": 2}],
            created_by=self.chrisis,
        )


class TheInvoiceShowsWhatTheSchoolChose(InvoiceSetup):
    """F34. A school ordered a kit — it should see a kit."""

    def setUp(self):
        super().setUp()
        self.order = self.an_order()
        self.client.force_authenticate(self.chrisis)

    def fetch(self):
        return self.client.get(
            reverse("orders:school-order-invoice", args=[self.order.pk])
        )

    def test_the_invoice_number_is_the_order_number(self):
        """AsOne treats the two as one thing — the school uses it to hand the
        right parcel to the right child."""
        self.assertEqual(self.fetch().data["number"], self.order.number)

    def test_it_carries_the_student_name_date_and_status(self):
        data = self.fetch().data

        self.assertEqual(data["student_name"], "Miriam Achieng")
        self.assertEqual(data["order_date"], ORDERED_ON.isoformat())
        self.assertEqual(data["status"], OrderStatus.HOLD)

    def test_a_kit_appears_as_a_kit_with_its_garments_beneath(self):
        data = self.fetch().data

        self.assertEqual(len(data["kits"]), 1)
        group = data["kits"][0]
        self.assertEqual(group["kit_number"], "PS-STARTER")
        self.assertEqual(len(group["lines"]), 2)

    def test_the_kit_carries_its_own_subtotal(self):
        # 2 shirts @ 25000 + 1 tunic @ 30000
        self.assertEqual(Decimal(self.fetch().data["kits"][0]["subtotal"]), Decimal("80000.00"))

    def test_individually_ordered_items_are_listed_separately(self):
        data = self.fetch().data

        self.assertEqual(len(data["items"]), 1)
        self.assertEqual(data["items"][0]["sku_number"], self.socks.number)

    def test_the_total_covers_both(self):
        # kit 80000 + 2 socks @ 5000
        self.assertEqual(Decimal(self.fetch().data["total"]), Decimal("90000.00"))

    def test_an_order_with_no_kit_has_an_empty_kit_section(self):
        self.order = self.an_order(with_kit=False)

        data = self.fetch().data
        self.assertEqual(data["kits"], [])
        self.assertEqual(len(data["items"]), 1)

    def test_grouping_does_not_change_the_order_itself(self):
        """Presentation only. The warehouse still picks individual SKUs."""
        self.fetch()

        self.order.refresh_from_db()
        self.assertEqual(self.order.lines.count(), 3)


class CancellingAnUnpaidInvoice(InvoiceSetup):
    """F36."""

    def setUp(self):
        super().setUp()
        self.order = self.an_order()
        self.client.force_authenticate(self.chrisis)

    def cancel(self, reason="Parent changed their mind"):
        return self.client.post(
            reverse("orders:school-order-cancel", args=[self.order.pk]),
            {"reason": reason},
            format="json",
        )

    def test_a_school_can_cancel_its_own_unpaid_order(self):
        response = self.cancel()

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.CANCELLED)

    def test_the_order_is_not_deleted(self):
        """A parent is holding that number."""
        self.cancel()

        self.assertTrue(SchoolOrder.objects.filter(pk=self.order.pk).exists())

    def test_who_cancelled_it_and_when_are_recorded(self):
        self.cancel()

        self.order.refresh_from_db()
        self.assertEqual(self.order.cancelled_by, self.chrisis)
        self.assertIsNotNone(self.order.cancelled_at)

    def test_the_reason_is_kept(self):
        self.cancel(reason="Parent changed their mind")

        self.order.refresh_from_db()
        self.assertEqual(self.order.cancellation_reason, "Parent changed their mind")

    def test_a_reason_is_optional(self):
        response = self.client.post(
            reverse("orders:school-order-cancel", args=[self.order.pk]), {}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_the_invoice_shows_it_was_cancelled(self):
        self.cancel()

        data = self.client.get(
            reverse("orders:school-order-invoice", args=[self.order.pk])
        ).data
        self.assertEqual(data["status"], OrderStatus.CANCELLED)
        self.assertIsNotNone(data["cancelled_at"])

    def test_cancelling_twice_is_refused(self):
        """It would overwrite the only record of who decided, and when."""
        self.cancel()

        self.assertEqual(self.cancel().status_code, status.HTTP_400_BAD_REQUEST)

    def test_a_released_order_cannot_be_cancelled_by_the_school(self):
        """F36 says an *unpaid* invoice. Past that, whether picked stock goes
        back and what happens to money taken is open question Q5."""
        self.order.status = OrderStatus.RELEASED
        self.order.save(update_fields=["status"])

        with self.assertRaises(CannotCancel):
            cancel_order(self.order, cancelled_by=self.chrisis)

    def test_the_refusal_says_why(self):
        self.order.status = OrderStatus.SHIPPED
        self.order.save(update_fields=["status"])

        response = self.cancel()
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("shipped", str(response.data["detail"]).lower())

    def test_a_school_cannot_cancel_another_schools_order(self):
        self.client.force_authenticate(self.peter)

        self.assertEqual(self.cancel().status_code, status.HTTP_404_NOT_FOUND)


class WhoCanSeeAnInvoice(InvoiceSetup):
    """F34 gives Finance a view. Placing and cancelling stay School Staff only."""

    def setUp(self):
        super().setUp()
        self.order = self.an_order()

    def invoice_as(self, user):
        self.client.force_authenticate(user)
        return self.client.get(
            reverse("orders:school-order-invoice", args=[self.order.pk])
        ).status_code

    def test_the_school_can_see_it(self):
        self.assertEqual(self.invoice_as(self.chrisis), status.HTTP_200_OK)

    def test_finance_can_see_it(self):
        """View only, per F34 — the gap that would otherwise block them."""
        self.assertEqual(self.invoice_as(self.finance), status.HTTP_200_OK)

    def test_warehouse_staff_cannot(self):
        self.assertEqual(self.invoice_as(self.clerk), status.HTTP_403_FORBIDDEN)

    def test_finance_cannot_cancel_it(self):
        """Reading is not the same as acting. F36 is School Staff only."""
        self.client.force_authenticate(self.finance)

        response = self.client.post(
            reverse("orders:school-order-cancel", args=[self.order.pk]), {}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_finance_cannot_place_an_order(self):
        self.client.force_authenticate(self.finance)

        response = self.client.post(
            reverse("orders:school-order-list"),
            {
                "student_name": "Someone",
                "order_date": ORDERED_ON.isoformat(),
                "skus": [{"sku": self.socks.pk, "quantity": 1}],
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)


class OrdersStillOnHold(InvoiceSetup):
    """F53 — invoices raised but not yet paid or released."""

    def setUp(self):
        super().setUp()
        self.mine = self.an_order(name="Miriam")
        self.also_mine = self.an_order(name="Grace")
        self.theirs = place_order(
            school=self.other_school,
            student_name="Joan",
            order_date=ORDERED_ON,
            skus=[{"sku": self.socks, "quantity": 1}],
            created_by=self.peter,
        )

    def fetch_as(self, user):
        self.client.force_authenticate(user)
        return self.client.get(reverse("orders:orders-on-hold"))

    def numbers_in(self, response):
        rows = response.data["results"] if "results" in response.data else response.data
        return sorted(row["number"] for row in rows)

    def test_a_school_sees_only_its_own(self):
        response = self.fetch_as(self.chrisis)

        self.assertEqual(
            self.numbers_in(response), sorted([self.mine.number, self.also_mine.number])
        )

    def test_a_lead_sees_every_school(self):
        """Wider than the point of sale itself — leads read this but cannot
        place an order."""
        response = self.fetch_as(self.lead)

        self.assertEqual(len(self.numbers_in(response)), 3)

    def test_finance_sees_every_school(self):
        self.assertEqual(len(self.numbers_in(self.fetch_as(self.finance))), 3)

    def test_warehouse_staff_may_not_read_it(self):
        """The F53 row leaves the warehouse column blank."""
        self.assertEqual(
            self.fetch_as(self.clerk).status_code, status.HTTP_403_FORBIDDEN
        )

    def test_a_cancelled_order_drops_off_the_queue(self):
        cancel_order(self.mine, cancelled_by=self.chrisis)

        self.assertNotIn(self.mine.number, self.numbers_in(self.fetch_as(self.chrisis)))

    def test_a_released_order_drops_off_too(self):
        """It is no longer waiting on a parent."""
        self.also_mine.status = OrderStatus.RELEASED
        self.also_mine.save(update_fields=["status"])

        self.assertNotIn(
            self.also_mine.number, self.numbers_in(self.fetch_as(self.chrisis))
        )

    def test_each_row_carries_what_is_owed(self):
        rows = self.fetch_as(self.chrisis).data
        rows = rows["results"] if "results" in rows else rows
        row = next(r for r in rows if r["number"] == self.mine.number)

        self.assertEqual(Decimal(row["total"]), Decimal("90000.00"))
        self.assertEqual(row["line_count"], 3)
