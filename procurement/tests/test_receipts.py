"""Receipts (F19, F20, F21).

AsOne's flow, and what each step protects:

    Enter    what arrived, alongside what the TC's handwritten packing list
             claimed — so a difference is visible, not argued away (F20)
    Post     one permanent ledger row per line, raising stock (F21)

Entering and posting are separate because the pack has the warehouse *check
the delivery against the packing list and resolve differences* in between.
"""

from datetime import date, timedelta
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.test import TestCase

from accounts.models import User
from accounts.tests.factories import build_sites, make_user
from catalog.models import Garment, GarmentPrice, Size, Sku, TailoringCenter
from inventory.models import MovementType, StockMovement, StockStatus
from inventory.services import stock_level
from procurement.services import (
    NotOnTheOrder,
    OrderHasNoLines,
    ReceiptAlreadyPosted,
    create_production_order,
    create_receipt,
    outstanding_on_order,
    post_receipt,
)

ORDER_DATE = date(2026, 9, 1)
DELIVERY_DATE = date(2026, 10, 15)


class ReceiptSetup(TestCase):
    def setUp(self):
        self.sites = build_sites()
        self.lead = make_user("sharon", User.Role.PROGRAM_LEAD)
        self.clerk = make_user(
            "julius", User.Role.WAREHOUSE_STAFF, warehouse=self.sites["namayemba"]
        )
        # build_sites() already creates Idudi — reuse it rather than
        # colliding with its unique name.
        self.tc, _ = TailoringCenter.objects.get_or_create(name="Idudi")

        self.shirt = self.make_sku("White Shirt", "25000.00")
        self.socks = self.make_sku("Socks", "5000.00")

        self.order = create_production_order(
            created_by=self.lead,
            lines=[
                {"sku": self.shirt, "quantity": 500},
                {"sku": self.socks, "quantity": 200},
            ],
            order_date=ORDER_DATE,
            tailoring_center=self.tc,
            warehouse=self.sites["namayemba"],
        )

    def make_sku(self, name, amount):
        garment = Garment.objects.create(name=name)
        GarmentPrice.objects.create(
            garment=garment, unit_price=Decimal(amount), active_date=date(2026, 1, 1)
        )
        size = Size.objects.create(name=f"{name[:3]}-10", sort_order=10)
        return Sku.objects.create(garment=garment, size=size)

    #: Distinguishes "no lines given" from "deliberately empty" — `lines or
    #: default` would quietly substitute the default for an empty list, and
    #: the test asserting empty receipts are refused would pass for the
    #: wrong reason.
    NOT_GIVEN = object()

    def receive(self, lines=NOT_GIVEN, **fields):
        if lines is self.NOT_GIVEN:
            lines = [{"sku": self.shirt, "quantity_received": 500}]

        return create_receipt(
            production_order=self.order,
            lines=lines,
            created_by=self.clerk,
            packing_list_number=fields.pop("packing_list_number", "IDUDI/2026/014"),
            date_received=fields.pop("date_received", DELIVERY_DATE),
            **fields,
        )


class EnteringADelivery(ReceiptSetup):
    """F19. Recording what arrived changes nothing about stock."""

    def test_a_receipt_takes_its_warehouse_and_supplier_from_the_order(self):
        """Re-keying them would let a receipt claim goods landed elsewhere."""
        receipt = self.receive()

        self.assertEqual(receipt.warehouse, self.sites["namayemba"])
        self.assertEqual(receipt.tailoring_center, self.tc)

    def test_entering_a_receipt_does_not_move_stock(self):
        self.receive()

        self.assertEqual(stock_level(self.shirt, self.sites["namayemba"]), 0)
        self.assertEqual(StockMovement.objects.count(), 0)

    def test_a_receipt_is_numbered(self):
        self.assertTrue(self.receive().number.startswith("RC-"))

    def test_a_receipt_needs_lines(self):
        with self.assertRaises(OrderHasNoLines):
            self.receive(lines=[])

    def test_a_sku_not_on_the_order_is_refused(self):
        """A TC shipping something nobody ordered needs a person, not a clerk
        quietly absorbing it into stock."""
        stray = self.make_sku("Blazer", "40000.00")

        with self.assertRaises(NotOnTheOrder):
            self.receive(lines=[{"sku": stray, "quantity_received": 10}])

    def test_goods_cannot_arrive_before_the_order_was_placed(self):
        with self.assertRaises(ValidationError):
            self.receive(date_received=ORDER_DATE - timedelta(days=1))

    def test_the_packing_list_number_is_kept_as_written(self):
        """It is a number somebody wrote by hand on a sheet of paper."""
        receipt = self.receive(packing_list_number="idudi/14b")

        self.assertEqual(receipt.packing_list_number, "idudi/14b")


class DiscrepancyHandling(ReceiptSetup):
    """F20. The count is the truth; the paper is what the TC believed."""

    def test_a_short_delivery_records_the_difference(self):
        receipt = self.receive(
            lines=[
                {
                    "sku": self.shirt,
                    "quantity_received": 480,
                    "quantity_on_packing_list": 500,
                    "discrepancy_note": "Twenty short, counted twice.",
                }
            ]
        )

        line = receipt.lines.get()
        self.assertEqual(line.discrepancy, -20)
        self.assertTrue(receipt.has_discrepancy)

    def test_an_over_delivery_records_a_positive_difference(self):
        receipt = self.receive(
            lines=[
                {
                    "sku": self.shirt,
                    "quantity_received": 505,
                    "quantity_on_packing_list": 500,
                }
            ]
        )
        self.assertEqual(receipt.lines.get().discrepancy, 5)

    def test_agreement_shows_no_discrepancy(self):
        receipt = self.receive(
            lines=[
                {
                    "sku": self.shirt,
                    "quantity_received": 500,
                    "quantity_on_packing_list": 500,
                }
            ]
        )
        self.assertFalse(receipt.has_discrepancy)

    def test_a_silent_packing_list_is_not_a_disagreement(self):
        """"The paper did not say" is a different fact from "the paper agreed"."""
        receipt = self.receive(
            lines=[{"sku": self.shirt, "quantity_received": 480}]
        )

        line = receipt.lines.get()
        self.assertIsNone(line.quantity_on_packing_list)
        self.assertEqual(line.discrepancy, 0)

    def test_the_count_is_what_reaches_stock_not_the_paper(self):
        receipt = self.receive(
            lines=[
                {
                    "sku": self.shirt,
                    "quantity_received": 480,
                    "quantity_on_packing_list": 500,
                }
            ]
        )
        post_receipt(receipt, posted_by=self.clerk)

        self.assertEqual(stock_level(self.shirt, self.sites["namayemba"]), 480)


class PostingToInventory(ReceiptSetup):
    """F21. Posting is what raises stock."""

    def test_posting_raises_stock(self):
        post_receipt(self.receive(), posted_by=self.clerk)

        self.assertEqual(stock_level(self.shirt, self.sites["namayemba"]), 500)

    def test_posting_writes_one_ledger_row_per_line(self):
        receipt = self.receive(
            lines=[
                {"sku": self.shirt, "quantity_received": 500},
                {"sku": self.socks, "quantity_received": 200},
            ]
        )
        movements = post_receipt(receipt, posted_by=self.clerk)

        self.assertEqual(len(movements), 2)
        self.assertEqual(StockMovement.objects.count(), 2)

    def test_the_ledger_row_records_who_posted_it(self):
        post_receipt(self.receive(), posted_by=self.clerk)

        self.assertEqual(StockMovement.objects.get().created_by, self.clerk)

    def test_stock_is_valued_at_the_order_price_not_the_price_list(self):
        """AsOne buys at an agreed figure; the stock is worth what was paid."""
        from catalog.services import reprice

        reprice(self.shirt.garment, Decimal("99000.00"), date(2026, 10, 1))
        post_receipt(self.receive(), posted_by=self.clerk)

        self.assertEqual(StockMovement.objects.get().unit_value, Decimal("25000.00"))

    def test_the_movement_names_the_receipt_and_the_route(self):
        receipt = self.receive()
        post_receipt(receipt, posted_by=self.clerk)

        movement = StockMovement.objects.get()
        self.assertEqual(movement.document_number, receipt.number)
        self.assertEqual(movement.source, "Idudi")
        self.assertEqual(movement.destination, "Namayemba")
        self.assertEqual(movement.movement_type, MovementType.RECEIPT)
        self.assertEqual(movement.stock_status, StockStatus.AVAILABLE)

    def test_the_movement_is_dated_when_the_goods_arrived(self):
        """Not when someone got round to keying it in."""
        post_receipt(self.receive(), posted_by=self.clerk)

        self.assertEqual(StockMovement.objects.get().occurred_on, DELIVERY_DATE)

    def test_posting_twice_is_refused(self):
        """It would double the stock, and the ledger cannot be taken back."""
        receipt = self.receive()
        post_receipt(receipt, posted_by=self.clerk)

        with self.assertRaises(ReceiptAlreadyPosted):
            post_receipt(receipt, posted_by=self.clerk)

    def test_a_refused_second_posting_leaves_stock_alone(self):
        receipt = self.receive()
        post_receipt(receipt, posted_by=self.clerk)
        try:
            post_receipt(receipt, posted_by=self.clerk)
        except ReceiptAlreadyPosted:
            pass

        self.assertEqual(stock_level(self.shirt, self.sites["namayemba"]), 500)

    def test_two_deliveries_against_one_order_both_count(self):
        """A TC delivering in two vans produces two receipts."""
        post_receipt(
            self.receive(lines=[{"sku": self.shirt, "quantity_received": 300}]),
            posted_by=self.clerk,
        )
        post_receipt(
            self.receive(lines=[{"sku": self.shirt, "quantity_received": 200}]),
            posted_by=self.clerk,
        )

        self.assertEqual(stock_level(self.shirt, self.sites["namayemba"]), 500)


class OutstandingOnOrder(ReceiptSetup):
    """What is still to come — ordered minus what actually landed."""

    def test_nothing_received_leaves_everything_outstanding(self):
        rows = {row["sku"]: row for row in outstanding_on_order(self.order)}

        self.assertEqual(rows[self.shirt]["outstanding"], 500)
        self.assertEqual(rows[self.socks]["outstanding"], 200)

    def test_a_posted_receipt_reduces_what_is_outstanding(self):
        post_receipt(
            self.receive(lines=[{"sku": self.shirt, "quantity_received": 300}]),
            posted_by=self.clerk,
        )

        rows = {row["sku"]: row for row in outstanding_on_order(self.order)}
        self.assertEqual(rows[self.shirt]["received"], 300)
        self.assertEqual(rows[self.shirt]["outstanding"], 200)

    def test_an_unposted_receipt_does_not_count(self):
        """Paperwork somebody is still checking is not goods on the shelf."""
        self.receive(lines=[{"sku": self.shirt, "quantity_received": 300}])

        rows = {row["sku"]: row for row in outstanding_on_order(self.order)}
        self.assertEqual(rows[self.shirt]["received"], 0)
        self.assertEqual(rows[self.shirt]["outstanding"], 500)

    def test_a_short_delivery_leaves_the_balance_outstanding(self):
        post_receipt(
            self.receive(
                lines=[
                    {
                        "sku": self.shirt,
                        "quantity_received": 480,
                        "quantity_on_packing_list": 500,
                    }
                ]
            ),
            posted_by=self.clerk,
        )

        rows = {row["sku"]: row for row in outstanding_on_order(self.order)}
        self.assertEqual(rows[self.shirt]["outstanding"], 20)
