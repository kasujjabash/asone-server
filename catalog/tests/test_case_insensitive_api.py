"""A case-duplicate must be a 400, not a 500.

The names in this project are guarded by **functional** constraints —
`UniqueConstraint(Lower("name"))`. DRF builds its uniqueness validators from
`unique=True` on a field and cannot see a functional one, so adding those
constraints silently turned every duplicate into an IntegrityError and a 500.

Every endpoint below is covered by `CaseInsensitiveUniqueValidator` (or, for
Garment, a serializer-level check, because its constraint spans two columns).

**If you add a functional unique constraint to a new model, add it here.**
The rule is enforced by the database either way; this is about whether the
client is told "that name is taken" or "the server broke".
"""

from django.db import transaction
from django.test import TestCase
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APIClient

from accounts.models import User
from accounts.tests.factories import build_sites, make_user
from catalog.models import Garment, Kit, School, Size, TailoringCenter
from inventory.models import ReasonCode


class CaseDuplicatesAreRejectedCleanly(TestCase):
    def setUp(self):
        self.sites = build_sites()
        self.client = APIClient()
        self.client.force_authenticate(make_user("sharon", User.Role.PROGRAM_LEAD))

        Size.objects.create(name="S", sort_order=1)
        Garment.objects.create(name="White Shirt", school_level=Garment.SchoolLevel.PRIMARY)
        Kit.objects.create(
            kit_number="PS-01", name="Starter", school_level=Kit.SchoolLevel.PRIMARY
        )
        ReasonCode.objects.create(
            code="DMG", name="Damaged", direction=ReasonCode.AdjustmentDirection.DECREASE
        )

    def post(self, route, body):
        """Each POST in its own block.

        An IntegrityError poisons the surrounding transaction, so without
        this a single failure makes every later assertion report a
        misleading TransactionManagementError.
        """
        with transaction.atomic():
            return self.client.post(reverse(route), body, format="json")

    def assertRejected(self, route, body):
        response = self.post(route, body)
        self.assertEqual(
            response.status_code,
            status.HTTP_400_BAD_REQUEST,
            msg=f"{route} returned {response.status_code} for a case-duplicate",
        )

    def test_warehouse(self):
        self.assertRejected("catalog:warehouse-list", {"name": "namayemba"})

    def test_school(self):
        school = School.objects.first()
        self.assertRejected(
            "catalog:school-list",
            {
                "name": school.name.lower(),
                "level": School.Level.PRIMARY,
                "primary_warehouse": self.sites["namayemba"].pk,
            },
        )

    def test_tailoring_center(self):
        name = TailoringCenter.objects.first().name.lower()
        self.assertRejected("catalog:tailoring-center-list", {"name": name})

    def test_size(self):
        self.assertRejected("catalog:size-list", {"name": "s"})

    def test_garment(self):
        """Its constraint spans Lower(name) AND school_level."""
        self.assertRejected(
            "catalog:garment-list", {"name": "white shirt", "school_level": "PS"}
        )

    def test_kit(self):
        self.assertRejected(
            "catalog:kit-list",
            {"kit_number": "ps-01", "name": "Other", "school_level": "PS"},
        )

    def test_reason_code(self):
        self.assertRejected(
            "inventory:reason-code-list", {"code": "dmg", "name": "Other"}
        )


class TheValidatorDoesNotBlockLegitimateWork(TestCase):
    """A validator that rejects too much is worse than the 500 it replaced."""

    def setUp(self):
        self.sites = build_sites()
        self.client = APIClient()
        self.client.force_authenticate(make_user("sharon", User.Role.PROGRAM_LEAD))

    def test_a_genuinely_new_name_is_accepted(self):
        response = self.client.post(
            reverse("catalog:warehouse-list"), {"name": "Bugiri"}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    def test_editing_a_row_does_not_trip_over_its_own_name(self):
        """The classic off-by-one in a uniqueness check."""
        warehouse = self.sites["namayemba"]

        response = self.client.patch(
            reverse("catalog:warehouse-detail", args=[warehouse.pk]),
            {"name": warehouse.name, "address": "Namayemba, Bugiri District"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_the_same_garment_name_at_a_different_level_is_accepted(self):
        Garment.objects.create(name="White Shirt", school_level=Garment.SchoolLevel.PRIMARY)

        response = self.client.post(
            reverse("catalog:garment-list"),
            {"name": "White Shirt", "school_level": "HS"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    def test_editing_a_garment_does_not_trip_over_itself(self):
        garment = Garment.objects.create(
            name="White Shirt", school_level=Garment.SchoolLevel.PRIMARY
        )

        response = self.client.patch(
            reverse("catalog:garment-detail", args=[garment.pk]),
            {"colour": "White"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
