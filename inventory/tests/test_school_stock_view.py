"""A school looking at its warehouse's stock.

AsOne asked for schools to see inventory. What they meant is a school
watching the warehouse that serves it — "are my shirts there yet" — and not
stock held at the school. Schools hold none: the ledger has a warehouse
column and no school column, and nothing here changes that.

So the rule being protected is narrow and worth stating plainly:

    a school clerk reads stock levels, at one warehouse, and only theirs
"""

from datetime import date
from decimal import Decimal

from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from accounts.models import User
from accounts.tests.factories import build_sites, make_user
from catalog.models import Garment, GarmentPrice, Size, Sku
from inventory.models import MovementType
from inventory.services import post_movement

TODAY = date(2026, 11, 10)
Role = User.Role

#: The school, its warehouse, and the ledger sum. Asserted rather than
#: described, so a fourth one has to be argued for.
QUERIES_PER_REQUEST = 3


class SchoolStockSetup(APITestCase):
    def setUp(self):
        self.sites = build_sites()
        # school_a orders from Namayemba, school_b from Serere.
        self.chrisis = make_user("chrisis", Role.SCHOOL_STAFF, school=self.sites["school_a"])
        self.peter = make_user("peter", Role.SCHOOL_STAFF, school=self.sites["school_b"])
        self.clerk = make_user(
            "julius", Role.WAREHOUSE_STAFF, warehouse=self.sites["namayemba"]
        )

        self.shirt = self.sku("White Shirt")
        self.blazer = self.sku("Blazer")

        self.stock(self.shirt, 40, self.sites["namayemba"])
        self.stock(self.blazer, 7, self.sites["serere"])

    def sku(self, name):
        garment = Garment.objects.create(name=name)
        GarmentPrice.objects.create(
            garment=garment, unit_price=Decimal("25000.00"), active_date=date(2026, 1, 1)
        )
        return Sku.objects.create(
            garment=garment, size=Size.objects.create(name=f"{name[:6]}-10", sort_order=10)
        )

    def stock(self, sku, quantity, warehouse):
        post_movement(
            warehouse=warehouse,
            sku=sku,
            quantity=quantity,
            movement_type=MovementType.RECEIPT,
            unit_value=Decimal("25000.00"),
            document_number="RC-100001",
            occurred_on=TODAY,
            created_by=self.clerk,
        )

    def levels(self, user, **params):
        self.client.force_authenticate(user)
        return self.client.get(reverse("inventory:stock-levels"), params)


class ASchoolSeesItsOwnWarehouse(SchoolStockSetup):
    def test_a_school_clerk_may_read_stock_levels(self):
        self.assertEqual(self.levels(self.chrisis).status_code, status.HTTP_200_OK)

    def test_they_see_the_warehouse_that_serves_them(self):
        rows = self.levels(self.chrisis).data

        self.assertEqual({row["warehouse_name"] for row in rows}, {"Namayemba"})

    def test_they_do_not_see_another_warehouse(self):
        """Serere holds the blazers. Namayemba PS has no business seeing them."""
        rows = self.levels(self.chrisis).data

        self.assertNotIn(self.blazer.number, {row["sku_number"] for row in rows})

    def test_asking_for_another_warehouse_by_id_changes_nothing(self):
        """`?warehouse=` is honoured for the all-locations roles and ignored
        for a school — otherwise the scoping would be a suggestion."""
        rows = self.levels(self.chrisis, warehouse=self.sites["serere"].pk).data

        self.assertEqual({row["warehouse_name"] for row in rows}, {"Namayemba"})

    def test_two_schools_on_two_warehouses_see_different_stock(self):
        mine = {row["sku_number"] for row in self.levels(self.chrisis).data}
        theirs = {row["sku_number"] for row in self.levels(self.peter).data}

        self.assertEqual(mine, {self.shirt.number})
        self.assertEqual(theirs, {self.blazer.number})


class TheSchoolPathCostsAFixedNumberOfQueries(SchoolStockSetup):
    """Reaching the warehouse through the school adds two lookups — the
    school, then its warehouse. Both are per request, not per row, and this
    is what says so when someone adds a third."""

    def test_more_stock_does_not_mean_more_queries(self):
        # Refetched deliberately. `self.chrisis` was built in setUp and has
        # its school cached in memory, which would hide the two lookups this
        # test exists to count — a real request authenticates from a token
        # and loads the user cold.
        cold = User.objects.get(pk=self.chrisis.pk)
        self.client.force_authenticate(cold)
        url = reverse("inventory:stock-levels")

        with self.assertNumQueries(QUERIES_PER_REQUEST):
            self.client.get(url)

        for name in ("Socks", "Tunic", "Shorts", "Jumper"):
            self.stock(self.sku(name), 5, self.sites["namayemba"])

        cold = User.objects.get(pk=self.chrisis.pk)
        self.client.force_authenticate(cold)
        with self.assertNumQueries(QUERIES_PER_REQUEST):
            self.client.get(url)


class ReadingIsAllTheyGet(SchoolStockSetup):
    """Seeing stock is not touching it."""

    def test_a_school_clerk_cannot_post_a_movement(self):
        self.client.force_authenticate(self.chrisis)

        response = self.client.post(reverse("inventory:stock-levels"), {})
        self.assertIn(
            response.status_code,
            (status.HTTP_403_FORBIDDEN, status.HTTP_405_METHOD_NOT_ALLOWED),
        )

    def test_a_school_clerk_still_cannot_read_the_movement_ledger(self):
        """Stock levels answer "is it there". The ledger is the audit trail,
        and AsOne gave that to the warehouses and Finance."""
        self.client.force_authenticate(self.chrisis)

        self.assertEqual(
            self.client.get(reverse("inventory:movement-list")).status_code,
            status.HTTP_403_FORBIDDEN,
        )


class ABrokenAccountSeesNothing(SchoolStockSetup):
    """`User.clean()` refuses to save a school user with no school, but
    nothing stops `objects.create()`. If one exists, the wrong answer is
    every warehouse in the country."""

    def test_a_school_user_with_no_school_gets_an_empty_list(self):
        stray = User.objects.create_user(
            email="stray@asone.test", password="x" * 20, role=Role.SCHOOL_STAFF
        )

        response = self.levels(stray)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(list(response.data), [])
