"""Adjustments, costed — F58.

The half that is buildable today: units and value, straight from the ledger.

The half that is not is open question Q6 — AsOne's p.6 marks the financial
treatment of damaged stock "To be determined". These tests pin the seam so
that whoever answers it can see exactly what changes, and so nobody mistakes
"not yet classified" for a bug.
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
from inventory.models import MovementType, ReasonCode
from inventory.reports import UNCLASSIFIED, adjustments_costed
from inventory.services import create_adjustment, post_adjustment, post_movement

IN_FORCE = date(2026, 1, 1)
ON = date(2026, 10, 20)
Role = User.Role
Direction = ReasonCode.AdjustmentDirection


class CostedSetup(APITestCase):
    def setUp(self):
        self.sites = build_sites()
        self.warehouse = self.sites["namayemba"]
        self.other = self.sites["serere"]

        self.finance = make_user("musana", Role.FINANCE)
        self.julius = make_user(
            "julius", Role.WAREHOUSE_STAFF, warehouse=self.warehouse
        )

        self.shirt = self.priced_sku("White Shirt", "25000.00")

        self.damaged = ReasonCode.objects.create(
            code="DMG", name="Damaged", direction=Direction.DECREASE
        )
        self.returned = ReasonCode.objects.create(
            code="RET", name="Return", direction=Direction.INCREASE
        )

    def priced_sku(self, name, price):
        garment = Garment.objects.create(name=name)
        GarmentPrice.objects.create(
            garment=garment, unit_price=Decimal(price), active_date=IN_FORCE
        )
        return Sku.objects.create(
            garment=garment,
            size=Size.objects.create(name=f"{name[:6]}-10", sort_order=10),
        )

    def stock(self, quantity, warehouse=None):
        post_movement(
            warehouse=warehouse or self.warehouse,
            sku=self.shirt,
            quantity=quantity,
            movement_type=MovementType.RECEIPT,
            unit_value=Decimal("25000.00"),
            document_number="RC-100001",
            occurred_on=IN_FORCE,
            created_by=self.julius,
        )

    def adjust(self, quantity, reason, warehouse=None, on=ON):
        adjustment = create_adjustment(
            warehouse=warehouse or self.warehouse,
            sku=self.shirt,
            quantity=quantity,
            reason_code=reason,
            adjustment_date=on,
            created_by=self.finance,
        )
        return post_adjustment(adjustment, posted_by=self.finance)


class WhatTheAdjustmentsWereWorth(CostedSetup):
    def test_a_damage_shows_as_a_negative_value(self):
        """Stock left, so the value of stock fell. The sign is the point —
        the rows have to sum to the net effect."""
        self.stock(100)
        self.adjust(4, self.damaged)

        row = next(r for r in adjustments_costed() if r["reason_code"] == "DMG")

        self.assertEqual(row["units"], -4)
        self.assertEqual(row["value"], Decimal("-100000.00"))

    def test_a_return_shows_as_a_positive_value(self):
        self.stock(100)
        self.adjust(2, self.returned)

        row = next(r for r in adjustments_costed() if r["reason_code"] == "RET")

        self.assertEqual(row["units"], 2)
        self.assertEqual(row["value"], Decimal("50000.00"))

    def test_adjustments_are_grouped_by_reason_code(self):
        self.stock(100)
        self.adjust(3, self.damaged)
        self.adjust(2, self.damaged)
        self.adjust(1, self.returned)

        rows = {r["reason_code"]: r for r in adjustments_costed()}

        self.assertEqual(rows["DMG"]["adjustments"], 2)
        self.assertEqual(rows["DMG"]["units"], -5)
        self.assertEqual(rows["RET"]["adjustments"], 1)

    def test_an_unposted_adjustment_is_not_counted(self):
        """It has moved nothing. The ledger is what happened."""
        self.stock(100)
        create_adjustment(
            warehouse=self.warehouse,
            sku=self.shirt,
            quantity=9,
            reason_code=self.damaged,
            adjustment_date=ON,
            created_by=self.finance,
        )

        self.assertEqual(adjustments_costed(), [])

    def test_receipts_are_not_adjustments(self):
        """Only ADJUSTMENT movements count — a delivery is not a write-off."""
        self.stock(100)

        self.assertEqual(adjustments_costed(), [])

    def test_it_can_be_narrowed_to_one_warehouse(self):
        self.stock(100)
        self.stock(100, warehouse=self.other)
        self.adjust(4, self.damaged)
        self.adjust(7, self.damaged, warehouse=self.other)

        rows = adjustments_costed(warehouse=self.other)

        self.assertEqual(rows[0]["units"], -7)

    def test_it_can_be_narrowed_to_a_period(self):
        self.stock(100)
        self.adjust(4, self.damaged, on=date(2026, 10, 5))
        self.adjust(6, self.damaged, on=date(2026, 11, 5))

        rows = adjustments_costed(
            date_from=date(2026, 11, 1), date_to=date(2026, 11, 30)
        )

        self.assertEqual(rows[0]["units"], -6)


class TheFinancialTreatmentIsTheOpenQuestion(CostedSetup):
    """Q6. The report says what the value was; it does not say what that
    means to the accounts, because AsOne has not decided."""

    def test_every_row_reads_unclassified_today(self):
        self.stock(100)
        self.adjust(4, self.damaged)

        self.assertEqual(adjustments_costed()[0]["treatment"], UNCLASSIFIED)

    @mock.patch.dict(
        "inventory.reports.FINANCIAL_TREATMENT", {"DMG": "Written off to expense"}
    )
    def test_answering_q6_is_one_dictionary_entry(self):
        """This is the whole change when AsOne answers — proven here so
        nobody rebuilds the report to add it."""
        self.stock(100)
        self.adjust(4, self.damaged)

        self.assertEqual(adjustments_costed()[0]["treatment"], "Written off to expense")


class CostedAdjustmentsApi(CostedSetup):
    def url(self):
        return reverse("inventory:adjustments-costed")

    def test_finance_may_read_it(self):
        self.client.force_authenticate(self.finance)

        self.assertEqual(self.client.get(self.url()).status_code, status.HTTP_200_OK)

    def test_a_lead_may_read_it(self):
        self.client.force_authenticate(make_user("sharon", Role.PROGRAM_LEAD))

        self.assertEqual(self.client.get(self.url()).status_code, status.HTTP_200_OK)

    def test_warehouse_staff_may_not(self):
        """A money report, not a warehouse one."""
        self.client.force_authenticate(self.julius)

        self.assertEqual(
            self.client.get(self.url()).status_code, status.HTTP_403_FORBIDDEN
        )

    def test_a_school_clerk_may_not(self):
        self.client.force_authenticate(
            make_user("chrisis", Role.SCHOOL_STAFF, school=self.sites["school_a"])
        )

        self.assertEqual(
            self.client.get(self.url()).status_code, status.HTTP_403_FORBIDDEN
        )

    def test_a_bad_date_is_a_400_naming_the_parameter(self):
        """Rather than a report quietly covering all time."""
        self.client.force_authenticate(self.finance)

        response = self.client.get(self.url(), {"from": "01/09/2026"})

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("from", response.data)
