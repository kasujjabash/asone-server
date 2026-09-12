"""The shipments list — F41's read side.

The endpoint a shipping screen is built on. Two things are worth testing
here and nothing else is: that it is **read only**, because a client able to
POST a shipment could claim goods left the building without the ledger
moving; and that the two-sided scoping is right, because a warehouse asks
"what did I send" and a school asks "what is coming to me", and those are
different columns on the same table.
"""

from datetime import date, timedelta
from decimal import Decimal

from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from accounts.models import User
from accounts.tests.factories import build_sites, make_user
from catalog.models import Garment, GarmentPrice, School, Size, Sku
from inventory.models import MovementType
from inventory.services import post_movement
from orders.services import (
    confirm_receipt,
    pick_order,
    place_order,
    release_order,
    ship_order,
)

IN_FORCE = date(2026, 1, 1)
TODAY = date(2026, 11, 10)
Role = User.Role


class ShipmentApiSetup(APITestCase):
    def setUp(self):
        self.sites = build_sites()
        self.namayemba = self.sites["namayemba"]
        self.serere = self.sites["serere"]
        self.school = self.sites["school_a"]

        self.julius = make_user("julius", Role.WAREHOUSE_STAFF, warehouse=self.namayemba)
        self.joan = make_user("joan", Role.WAREHOUSE_STAFF, warehouse=self.serere)
        self.lead = make_user("sharon", Role.PROGRAM_LEAD)
        self.finance = make_user("musana", Role.FINANCE)
        self.clerk = make_user("chrisis", Role.SCHOOL_STAFF, school=self.school)

        self.shirt = self.priced_sku("White Shirt", "25000.00")
        self.url = reverse("orders:shipment-list")

    def priced_sku(self, name, price):
        garment = Garment.objects.create(name=name)
        GarmentPrice.objects.create(
            garment=garment, unit_price=Decimal(price), active_date=IN_FORCE
        )
        return Sku.objects.create(
            garment=garment,
            size=Size.objects.create(name=f"S{Size.objects.count() + 1}", sort_order=10),
        )

    def stock(self, quantity, warehouse=None):
        post_movement(
            warehouse=warehouse or self.namayemba,
            sku=self.shirt,
            quantity=quantity,
            movement_type=MovementType.RECEIPT,
            unit_value=Decimal("25000.00"),
            document_number="RC-100001",
            occurred_on=IN_FORCE,
            created_by=self.julius,
        )

    def despatch(self, school=None, warehouse=None, by=None, quantity=2, on=TODAY):
        """An order taken all the way to Shipped, returning its shipment."""
        warehouse = warehouse or self.namayemba
        self.stock(quantity, warehouse)
        order = place_order(
            school=school or self.school,
            student_name="Miriam Achieng",
            order_date=TODAY,
            skus=[{"sku": self.shirt, "quantity": quantity}],
            created_by=self.clerk,
        )
        release_order(order, released_by=self.finance)
        pick_order(order, picked_by=by or self.julius)
        return ship_order(order, shipped_by=by or self.julius, shipped_on=on)


class ItIsReadOnly(ShipmentApiSetup):
    """Stock leaves the building through `ship_order`, never through a POST."""

    def test_a_warehouse_clerk_cannot_post_a_shipment(self):
        self.client.force_authenticate(self.julius)

        response = self.client.post(self.url, {}, format="json")

        self.assertEqual(response.status_code, status.HTTP_405_METHOD_NOT_ALLOWED)

    def test_a_lead_cannot_post_one_either(self):
        self.client.force_authenticate(self.lead)

        self.assertEqual(
            self.client.post(self.url, {}, format="json").status_code,
            status.HTTP_405_METHOD_NOT_ALLOWED,
        )


class WhoMayReadIt(ShipmentApiSetup):
    def test_a_lead_sees_every_warehouse(self):
        self.despatch()
        self.client.force_authenticate(self.lead)

        self.assertEqual(self.client.get(self.url).data["count"], 1)

    def test_a_school_may_read_its_own(self):
        self.despatch()
        self.client.force_authenticate(self.clerk)

        self.assertEqual(self.client.get(self.url).status_code, status.HTTP_200_OK)

    def test_finance_may_not(self):
        """The matrix gives Finance the costed reports, not the backlog."""
        self.client.force_authenticate(self.finance)

        self.assertEqual(
            self.client.get(self.url).status_code, status.HTTP_403_FORBIDDEN
        )


class ItIsScopedBothWays(ShipmentApiSetup):
    def test_a_clerk_sees_only_what_left_their_warehouse(self):
        self.despatch()
        self.client.force_authenticate(self.joan)

        self.assertEqual(self.client.get(self.url).data["count"], 0)

    def test_the_clerk_who_sent_it_sees_it(self):
        self.despatch()
        self.client.force_authenticate(self.julius)

        self.assertEqual(self.client.get(self.url).data["count"], 1)

    def test_a_school_does_not_see_another_schools_parcels(self):
        other = School.objects.create(
            name="Bugiri Primary", primary_warehouse=self.namayemba
        )
        self.despatch(school=other)
        self.client.force_authenticate(self.clerk)

        self.assertEqual(self.client.get(self.url).data["count"], 0)


class TheRowsCarryWhatTheScreenDraws(ShipmentApiSetup):
    def test_a_row_names_its_consignee_and_totals_its_units(self):
        shipment = self.despatch(quantity=3)
        self.client.force_authenticate(self.lead)

        row = self.client.get(self.url).data["results"][0]

        self.assertEqual(row["number"], shipment.number)
        self.assertEqual(row["school_name"], self.school.name)
        self.assertEqual(row["from_warehouse_name"], self.namayemba.name)
        self.assertEqual(row["total_quantity"], 3)

    def test_status_is_shipped_until_the_school_confirms(self):
        shipment = self.despatch()
        self.client.force_authenticate(self.lead)

        self.assertEqual(self.client.get(self.url).data["results"][0]["status"], "SHIPPED")

        confirm_receipt(shipment, confirmed_by=self.clerk)

        self.assertEqual(
            self.client.get(self.url).data["results"][0]["status"], "DELIVERED"
        )


class TheFiltersTheScreenOffers(ShipmentApiSetup):
    def test_status_narrows_to_what_is_still_out(self):
        delivered = self.despatch()
        confirm_receipt(delivered, confirmed_by=self.clerk)
        self.despatch()
        self.client.force_authenticate(self.lead)

        self.assertEqual(self.client.get(self.url, {"status": "SHIPPED"}).data["count"], 1)
        self.assertEqual(
            self.client.get(self.url, {"status": "DELIVERED"}).data["count"], 1
        )

    def test_school_narrows_to_one_consignee(self):
        other = School.objects.create(
            name="Bugiri Primary", primary_warehouse=self.namayemba
        )
        self.despatch()
        self.despatch(school=other)
        self.client.force_authenticate(self.lead)

        response = self.client.get(self.url, {"school": self.school.id})

        self.assertEqual(response.data["count"], 1)
        self.assertEqual(response.data["results"][0]["school_name"], self.school.name)

    def test_the_date_range_is_inclusive_at_both_ends(self):
        self.despatch(on=TODAY - timedelta(days=5))
        self.despatch(on=TODAY)
        self.client.force_authenticate(self.lead)

        both = self.client.get(
            self.url,
            {"shipped_from": str(TODAY - timedelta(days=5)), "shipped_to": str(TODAY)},
        )
        self.assertEqual(both.data["count"], 2)

        narrowed = self.client.get(self.url, {"shipped_from": str(TODAY)})
        self.assertEqual(narrowed.data["count"], 1)

    def test_a_date_that_will_not_parse_is_a_400_naming_the_parameter(self):
        """Rather than a filter that silently covers all time."""
        self.client.force_authenticate(self.lead)

        response = self.client.get(self.url, {"shipped_from": "01/09/2026"})

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("shipped_from", response.data)

    def test_search_finds_a_parcel_by_its_order_number(self):
        shipment = self.despatch()
        self.client.force_authenticate(self.lead)

        response = self.client.get(
            self.url, {"search": shipment.orders.first().number}
        )

        self.assertEqual(response.data["count"], 1)
