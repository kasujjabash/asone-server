"""Inventory adjustment reason codes (F13).

AsOne named four on p.3 — Return, Warehouse Transfers, Pick up or Loss,
Damaged — and then wrote "May be more…". That sentence is why this is a
database table Central Office maintains rather than a `TextChoices` in code.

The table carries no financial behaviour on purpose. p.6 gives each
adjustment kind a different financial note and marks Damages "To be
determined" (open question Q6), so there is nothing settled to build on yet.
"""

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import TestCase
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from accounts.models import User
from accounts.tests.factories import build_sites, make_user
from inventory.models import ReasonCode

Role = User.Role


class ReasonCodeRules(TestCase):
    def setUp(self):
        self.damaged = ReasonCode.objects.create(
            code="DMG", name="Damaged", direction=ReasonCode.AdjustmentDirection.DECREASE
        )

    def test_a_code_cannot_repeat(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            ReasonCode.objects.create(
                code="DMG",
                name="Damaged in transit",
                direction=ReasonCode.AdjustmentDirection.DECREASE,
            )

    def test_a_code_cannot_repeat_in_another_case(self):
        """"DMG" and "dmg" are the same code to everyone except the database."""
        with self.assertRaises(IntegrityError), transaction.atomic():
            ReasonCode.objects.create(
                code="dmg",
                name="Something else",
                direction=ReasonCode.AdjustmentDirection.DECREASE,
            )

    def test_a_name_cannot_repeat_either(self):
        """Two codes reading "Damaged" would make the picker ambiguous."""
        with self.assertRaises(IntegrityError), transaction.atomic():
            ReasonCode.objects.create(
                code="DMG2",
                name="damaged",
                direction=ReasonCode.AdjustmentDirection.DECREASE,
            )

    def test_a_code_is_retired_rather_than_deleted(self):
        self.damaged.is_active = False
        self.damaged.save(update_fields=["is_active"])

        self.damaged.refresh_from_db()
        self.assertFalse(self.damaged.is_active)
        self.assertTrue(ReasonCode.objects.filter(pk=self.damaged.pk).exists())

    def test_it_reads_usefully(self):
        self.assertEqual(str(self.damaged), "DMG — Damaged")

    def test_a_code_must_have_a_direction(self):
        """F23 reads this to decide the sign of an adjustment — it cannot be
        left blank the way description can.

        Enforced by full_clean() rather than a database constraint: a plain
        `.objects.create()` with no `direction` would silently save an empty
        string, since CharField has no NULL to refuse — this is a case where
        the model-level rule really is the only guard, not a backstop for a
        database one.
        """
        with self.assertRaises(ValidationError):
            ReasonCode(code="NEW", name="Something new").full_clean()


class ReasonCodeApi(APITestCase):
    """Master data: the leads maintain it, Finance reads it.

    The matrix gives the Inventory Adj column to Finance alone, and reason
    codes belong to that column — so warehouse and school staff get nothing.
    """

    def setUp(self):
        self.sites = build_sites()
        self.users = {
            Role.PROGRAM_LEAD: make_user("sharon", Role.PROGRAM_LEAD),
            Role.OPERATIONS_MANAGER: make_user("andrew", Role.OPERATIONS_MANAGER),
            Role.FINANCE: make_user("musana", Role.FINANCE),
            Role.WAREHOUSE_STAFF: make_user(
                "julius", Role.WAREHOUSE_STAFF, warehouse=self.sites["namayemba"]
            ),
            Role.SCHOOL_STAFF: make_user(
                "chrisis", Role.SCHOOL_STAFF, school=self.sites["school_a"]
            ),
        }
        ReasonCode.objects.create(
            code="DMG", name="Damaged", direction=ReasonCode.AdjustmentDirection.DECREASE
        )

    def as_role(self, role):
        self.client.force_authenticate(self.users[role])

    def test_leads_and_finance_may_read(self):
        for role in (Role.PROGRAM_LEAD, Role.OPERATIONS_MANAGER, Role.FINANCE):
            with self.subTest(role=role):
                self.as_role(role)
                self.assertEqual(
                    self.client.get(reverse("inventory:reason-code-list")).status_code,
                    status.HTTP_200_OK,
                )

    def test_warehouse_and_school_staff_may_not(self):
        for role in (Role.WAREHOUSE_STAFF, Role.SCHOOL_STAFF):
            with self.subTest(role=role):
                self.as_role(role)
                self.assertEqual(
                    self.client.get(reverse("inventory:reason-code-list")).status_code,
                    status.HTTP_403_FORBIDDEN,
                )

    def test_a_lead_can_add_a_code(self):
        """AsOne said "may be more" — adding one must not need a developer."""
        self.as_role(Role.PROGRAM_LEAD)

        response = self.client.post(
            reverse("inventory:reason-code-list"),
            {
                "code": "SPOIL",
                "name": "Spoiled in storage",
                "description": "Water damage.",
                "direction": "DECREASE",
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    def test_finance_may_read_but_not_add(self):
        self.as_role(Role.FINANCE)

        response = self.client.post(
            reverse("inventory:reason-code-list"),
            {"code": "SPOIL", "name": "Spoiled in storage", "direction": "DECREASE"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_a_duplicate_code_is_a_400_not_a_500(self):
        self.as_role(Role.PROGRAM_LEAD)

        response = self.client.post(
            reverse("inventory:reason-code-list"),
            {"code": "dmg", "name": "Another damaged", "direction": "DECREASE"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_a_code_cannot_be_created_without_a_direction(self):
        """F23 depends on this being set — the API must refuse it, not save
        a code nobody can post an adjustment against correctly."""
        self.as_role(Role.PROGRAM_LEAD)

        response = self.client.post(
            reverse("inventory:reason-code-list"),
            {"code": "SPOIL", "name": "Spoiled in storage"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("direction", response.data)

    def test_a_code_cannot_be_deleted(self):
        """Past adjustments will point at it. Retire it instead."""
        self.as_role(Role.PROGRAM_LEAD)
        code = ReasonCode.objects.get(code="DMG")

        response = self.client.delete(
            reverse("inventory:reason-code-detail", args=[code.pk])
        )
        self.assertEqual(response.status_code, status.HTTP_405_METHOD_NOT_ALLOWED)

    def test_retiring_one_works_through_the_api(self):
        self.as_role(Role.PROGRAM_LEAD)
        code = ReasonCode.objects.get(code="DMG")

        response = self.client.patch(
            reverse("inventory:reason-code-detail", args=[code.pk]),
            {"is_active": False},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        code.refresh_from_db()
        self.assertFalse(code.is_active)

    def test_active_codes_can_be_filtered(self):
        """An adjustment picker should offer only the codes still in use."""
        ReasonCode.objects.create(
            code="OLD",
            name="Retired reason",
            direction=ReasonCode.AdjustmentDirection.DECREASE,
            is_active=False,
        )
        self.as_role(Role.PROGRAM_LEAD)

        response = self.client.get(
            reverse("inventory:reason-code-list"), {"is_active": "true"}
        )
        codes = [row["code"] for row in response.data["results"]]
        self.assertIn("DMG", codes)
        self.assertNotIn("OLD", codes)
