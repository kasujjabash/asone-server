"""Costed reports for Finance (F55, F56).

Two rules the numbers depend on:

    Value comes from the document, not from today's price list.
    Only posted receipts count.

Both are easy to break by "improving" a report to look up the current price,
so each has a test that reprices afterwards and asserts the figure held.
"""

from datetime import date
from decimal import Decimal

from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from accounts.models import User
from accounts.tests.factories import build_sites, make_user
from catalog.models import Garment, GarmentPrice, Size, Sku, TailoringCenter
from catalog.services import reprice
from procurement.models.base import OrderStatus
from procurement.reports import (
    group_order_total,
    group_orders_costed,
    receipts_costed,
)
from procurement.services import (
    create_group_order,
    create_production_order,
    create_receipt,
    post_receipt,
)

ORDER_DATE = date(2026, 9, 1)
DELIVERY_DATE = date(2026, 10, 15)

Role = User.Role


class ReportSetup(APITestCase):
    def setUp(self):
        self.sites = build_sites()
        self.lead = make_user("sharon", Role.PROGRAM_LEAD)
        self.finance = make_user("musana", Role.FINANCE)
        self.clerk = make_user(
            "julius", Role.WAREHOUSE_STAFF, warehouse=self.sites["namayemba"]
        )
        self.school_staff = make_user(
            "chrisis", Role.SCHOOL_STAFF, school=self.sites["school_a"]
        )
        self.idudi, _ = TailoringCenter.objects.get_or_create(name="Idudi")
        self.serere_tc = TailoringCenter.objects.create(name="Serere TC")

        self.shirt = self.make_sku("White Shirt", "25000.00")

    def make_sku(self, name, amount):
        garment = Garment.objects.create(name=name)
        GarmentPrice.objects.create(
            garment=garment, unit_price=Decimal(amount), active_date=date(2026, 1, 1)
        )
        size = Size.objects.create(name=f"{name[:4]}-10", sort_order=10)
        return Sku.objects.create(garment=garment, size=size)

    def group_order(self, quantity=500, **fields):
        return create_group_order(
            created_by=self.lead,
            lines=[{"sku": self.shirt, "quantity": quantity}],
            order_date=ORDER_DATE,
            **fields,
        )

    def production_order(self, quantity=500, tc=None):
        return create_production_order(
            created_by=self.lead,
            lines=[{"sku": self.shirt, "quantity": quantity}],
            order_date=ORDER_DATE,
            tailoring_center=tc or self.idudi,
            warehouse=self.sites["namayemba"],
        )

    def deliver(self, order, received, post=True, on=DELIVERY_DATE):
        receipt = create_receipt(
            production_order=order,
            lines=[{"sku": self.shirt, "quantity_received": received}],
            created_by=self.clerk,
            packing_list_number="IDUDI/2026/014",
            date_received=on,
        )
        if post:
            post_receipt(receipt, posted_by=self.clerk)
        return receipt


class GroupOrdersCosted(ReportSetup):
    """F55 — what was committed to the Tailoring Centers."""

    def test_an_order_is_costed_at_its_line_prices(self):
        self.group_order(500)

        row = group_orders_costed()[0]
        self.assertEqual(row.quantity, 500)
        self.assertEqual(row.value, Decimal("12500000.00"))

    def test_repricing_afterwards_does_not_restate_the_report(self):
        """The figure is what was agreed on the day, not what it costs now."""
        self.group_order(500)
        reprice(self.shirt.garment, Decimal("99000.00"), date(2026, 12, 1))

        self.assertEqual(group_orders_costed()[0].value, Decimal("12500000.00"))

    def test_cancelled_orders_are_left_out(self):
        """No money was committed against a withdrawn order."""
        order = self.group_order(500)
        order.status = OrderStatus.CANCELLED
        order.save(update_fields=["status"])

        self.assertEqual(group_orders_costed().count(), 0)
        self.assertEqual(group_order_total()["value"], 0)

    def test_cancelled_orders_can_be_asked_for(self):
        order = self.group_order(500)
        order.status = OrderStatus.CANCELLED
        order.save(update_fields=["status"])

        self.assertEqual(group_orders_costed(include_cancelled=True).count(), 1)

    def test_a_period_narrows_the_report(self):
        self.group_order(500)

        self.assertEqual(group_orders_costed(date_from=date(2026, 10, 1)).count(), 0)
        self.assertEqual(group_orders_costed(date_to=date(2026, 9, 30)).count(), 1)

    def test_the_period_ends_are_inclusive(self):
        """Asking for September must include the 1st and the 30th."""
        self.group_order(500)

        self.assertEqual(
            group_orders_costed(date_from=ORDER_DATE, date_to=ORDER_DATE).count(), 1
        )

    def test_totals_add_up_across_orders(self):
        self.group_order(500)
        self.group_order(300)

        totals = group_order_total()
        self.assertEqual(totals["orders"], 2)
        self.assertEqual(totals["quantity"], 800)
        self.assertEqual(totals["value"], Decimal("20000000.00"))


class ReceiptsCosted(ReportSetup):
    """F56 — what each Tailoring Center actually delivered."""

    def test_a_delivery_is_costed_at_the_order_price(self):
        self.deliver(self.production_order(500), received=500)

        row = receipts_costed()[0]
        self.assertEqual(row["tailoring_center_name"], "Idudi")
        self.assertEqual(row["quantity"], 500)
        self.assertEqual(row["value"], Decimal("12500000.00"))

    def test_a_short_delivery_is_worth_what_arrived(self):
        """Not what the packing list claimed."""
        self.deliver(self.production_order(500), received=480)

        row = receipts_costed()[0]
        self.assertEqual(row["quantity"], 480)
        self.assertEqual(row["value"], Decimal("12000000.00"))

    def test_an_unposted_receipt_is_not_counted(self):
        """Paperwork somebody is still checking is not money owed."""
        self.deliver(self.production_order(500), received=500, post=False)

        self.assertEqual(len(list(receipts_costed())), 0)

    def test_repricing_afterwards_does_not_restate_it(self):
        self.deliver(self.production_order(500), received=500)
        reprice(self.shirt.garment, Decimal("99000.00"), date(2026, 12, 1))

        self.assertEqual(receipts_costed()[0]["value"], Decimal("12500000.00"))

    def test_deliveries_group_by_tailoring_center(self):
        self.deliver(self.production_order(500, tc=self.idudi), received=500)
        self.deliver(self.production_order(200, tc=self.serere_tc), received=200)

        rows = {r["tailoring_center_name"]: r for r in receipts_costed()}
        self.assertEqual(rows["Idudi"]["quantity"], 500)
        self.assertEqual(rows["Serere TC"]["quantity"], 200)

    def test_two_deliveries_from_one_tc_are_added_together(self):
        order = self.production_order(500)
        self.deliver(order, received=300)
        self.deliver(order, received=200)

        row = receipts_costed()[0]
        self.assertEqual(row["receipts"], 2)
        self.assertEqual(row["quantity"], 500)

    def test_it_can_be_narrowed_to_one_tailoring_center(self):
        self.deliver(self.production_order(500, tc=self.idudi), received=500)
        self.deliver(self.production_order(200, tc=self.serere_tc), received=200)

        rows = list(receipts_costed(tailoring_center=self.serere_tc.pk))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["tailoring_center_name"], "Serere TC")


class WhoCanReadTheReports(ReportSetup):
    """The Financial Reports column: the two leads and Finance."""

    ROUTES = ("procurement:group-orders-costed", "procurement:receipts-costed")

    def test_leads_and_finance_may_read(self):
        for user in (self.lead, self.finance):
            for route in self.ROUTES:
                with self.subTest(user=user.email, route=route):
                    self.client.force_authenticate(user)
                    self.assertEqual(
                        self.client.get(reverse(route)).status_code, status.HTTP_200_OK
                    )

    def test_warehouse_and_school_staff_may_not(self):
        """School Staff "cannot see costs beyond their own price list"."""
        for user in (self.clerk, self.school_staff):
            for route in self.ROUTES:
                with self.subTest(user=user.email, route=route):
                    self.client.force_authenticate(user)
                    self.assertEqual(
                        self.client.get(reverse(route)).status_code,
                        status.HTTP_403_FORBIDDEN,
                    )

    def test_they_require_authentication(self):
        for route in self.ROUTES:
            with self.subTest(route=route):
                self.client.force_authenticate(None)
                self.assertEqual(
                    self.client.get(reverse(route)).status_code,
                    status.HTTP_401_UNAUTHORIZED,
                )


class ReportApiShape(ReportSetup):
    def setUp(self):
        super().setUp()
        self.group_order(500)
        self.deliver(self.production_order(500), received=480)
        self.client.force_authenticate(self.finance)

    def test_group_orders_carry_totals_alongside_the_rows(self):
        response = self.client.get(reverse("procurement:group-orders-costed"))

        self.assertEqual(response.data["totals"]["quantity"], 500)
        self.assertEqual(len(response.data["orders"]), 1)

    def test_receipts_group_by_tc_and_can_be_drilled_into(self):
        summary = self.client.get(reverse("procurement:receipts-costed")).data
        self.assertNotIn("receipts", summary)

        detailed = self.client.get(
            reverse("procurement:receipts-costed"), {"detail": "true"}
        ).data
        self.assertEqual(len(detailed["receipts"]), 1)
        self.assertEqual(detailed["receipts"][0]["quantity"], 480)

    def test_a_malformed_date_is_a_400_not_a_500(self):
        response = self.client.get(
            reverse("procurement:group-orders-costed"), {"from": "not-a-date"}
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
