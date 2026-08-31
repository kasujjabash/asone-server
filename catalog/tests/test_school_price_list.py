"""A school works from its own price list — F29.

AsOne's wording: "School works from the Primary or High School price list."
The system knows which from the school on the user's account, so nobody has
to choose — and choosing wrongly is not possible.

Two things went wrong before this existed, and both were silent:

    a High School was served the Primary list, because PS was the default
    a Primary school could ask for HS and see garments it cannot order

The same endpoint still serves F51, the costed report, where leads and
Finance do name the list they want. These tests hold both behaviours apart.
"""

from datetime import date
from decimal import Decimal

from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from accounts.models import User
from accounts.tests.factories import make_user
from catalog.models import Garment, GarmentPrice, School, TailoringCenter, Warehouse

IN_FORCE = date(2026, 1, 1)
Role = User.Role
Level = Garment.SchoolLevel


class SchoolPriceListSetup(APITestCase):
    def setUp(self):
        centre = TailoringCenter.objects.create(name="Idudi")
        warehouse = Warehouse.objects.create(
            name="Namayemba", primary_tailoring_center=centre
        )
        self.primary_school = School.objects.create(
            name="Namayemba Primary", level=School.Level.PRIMARY, primary_warehouse=warehouse
        )
        self.high_school = School.objects.create(
            name="Bugiri High", level=School.Level.HIGH, primary_warehouse=warehouse
        )

        self.chrisis = make_user("chrisis", Role.SCHOOL_STAFF, school=self.primary_school)
        self.peter = make_user("peter", Role.SCHOOL_STAFF, school=self.high_school)
        self.lead = make_user("sharon", Role.PROGRAM_LEAD)
        self.finance = make_user("musana", Role.FINANCE)

        for name, level, price in (
            ("Blue Tunic", Level.PRIMARY, "30000.00"),
            ("Grey Shorts", Level.PRIMARY, "22000.00"),
            ("Navy Skirt", Level.HIGH, "32000.00"),
            ("Grey Trousers", Level.HIGH, "35000.00"),
            ("Socks", Level.BOTH, "5000.00"),
        ):
            garment = Garment.objects.create(name=name, school_level=level)
            GarmentPrice.objects.create(
                garment=garment, unit_price=Decimal(price), active_date=IN_FORCE
            )

    def fetch(self, user, level=None):
        self.client.force_authenticate(user)
        params = {"on": IN_FORCE.isoformat()}
        if level:
            params["level"] = level
        return self.client.get(reverse("catalog:price-list"), params)

    def garments_in(self, response):
        return sorted(row["garment"] for row in response.data)


class ASchoolGetsItsOwnListWithoutAsking(SchoolPriceListSetup):
    """The default used to be Primary for everyone."""

    def test_a_primary_school_gets_the_primary_list(self):
        response = self.fetch(self.chrisis)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            self.garments_in(response), ["Blue Tunic (PS)", "Grey Shorts (PS)", "Socks"]
        )

    def test_a_high_school_gets_the_high_school_list(self):
        """This was the silent failure: HS staff were served the PS list."""
        response = self.fetch(self.peter)

        self.assertEqual(
            self.garments_in(response),
            ["Grey Trousers (HS)", "Navy Skirt (HS)", "Socks"],
        )

    def test_the_two_schools_see_different_lists(self):
        self.assertNotEqual(
            self.garments_in(self.fetch(self.chrisis)),
            self.garments_in(self.fetch(self.peter)),
        )

    def test_a_garment_for_both_levels_appears_on_each(self):
        for user in (self.chrisis, self.peter):
            with self.subTest(user=user.email):
                self.assertIn("Socks", self.garments_in(self.fetch(user)))


class ASchoolCannotAskForTheOtherList(SchoolPriceListSetup):
    """Refused, not quietly corrected.

    A client sending the wrong level believes something untrue. Silently
    serving the right list would hide that until someone noticed the screen
    disagreed with the request.
    """

    def test_a_primary_school_asking_for_high_school_is_refused(self):
        response = self.fetch(self.chrisis, level="HS")

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_a_high_school_asking_for_primary_is_refused(self):
        response = self.fetch(self.peter, level="PS")

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_the_refusal_says_which_school_and_which_level(self):
        response = self.fetch(self.chrisis, level="HS")

        detail = str(response.data["detail"])
        self.assertIn("Namayemba Primary", detail)
        self.assertIn("Primary School", detail)

    def test_asking_for_its_own_level_explicitly_is_fine(self):
        """A frontend may well send it, and it is not wrong."""
        response = self.fetch(self.chrisis, level="PS")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            self.garments_in(response), ["Blue Tunic (PS)", "Grey Shorts (PS)", "Socks"]
        )

    def test_a_school_user_with_no_school_is_refused(self):
        """A misconfigured account has no price list, rather than a default one."""
        self.chrisis.school = None
        self.chrisis.save(update_fields=["school"])

        self.assertEqual(self.fetch(self.chrisis).status_code, status.HTTP_403_FORBIDDEN)


class EveryoneElseStillNamesTheList(SchoolPriceListSetup):
    """F51 — the report is unchanged. Scoping schools must not scope leads."""

    def test_a_lead_can_ask_for_either_list(self):
        self.assertEqual(
            self.garments_in(self.fetch(self.lead, level="PS")),
            ["Blue Tunic (PS)", "Grey Shorts (PS)", "Socks"],
        )
        self.assertEqual(
            self.garments_in(self.fetch(self.lead, level="HS")),
            ["Grey Trousers (HS)", "Navy Skirt (HS)", "Socks"],
        )

    def test_finance_can_ask_for_either_list(self):
        self.assertEqual(self.fetch(self.finance, level="HS").status_code, status.HTTP_200_OK)

    def test_an_unknown_level_is_still_a_400_for_them(self):
        response = self.fetch(self.lead, level="NONSENSE")

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
