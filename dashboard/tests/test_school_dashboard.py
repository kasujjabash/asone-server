"""The school's own dashboard — F62, the half that is not a warehouse.

The warehouse dashboard answers "what is in my building". A school has no
building, so this one answers "where is my paperwork". The tests worth having
are the ones that prove those two never get confused: that a school cannot
reach the warehouse dashboard, that a warehouse cannot reach this one, and
that every number here belongs to the school asking and to no other school.
"""

from datetime import date
from decimal import Decimal

from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from accounts.models import User
from accounts.tests.factories import build_sites, make_user
from catalog.models import Garment, GarmentPrice, School, Size, Sku
from inventory.models import MovementType
from inventory.services import post_movement
from dashboard import services
from orders.services import (
    cancel_order,
    confirm_receipt,
    pick_order,
    place_order,
    release_order,
    ship_order,
)

IN_FORCE = date(2026, 1, 1)
TODAY = date(2026, 11, 10)
Role = User.Role


class SchoolDashboardSetup(APITestCase):
    def setUp(self):
        self.sites = build_sites()
        self.namayemba = self.sites["namayemba"]
        self.school = self.sites["school_a"]

        self.julius = make_user("julius", Role.WAREHOUSE_STAFF, warehouse=self.namayemba)
        self.finance = make_user("musana", Role.FINANCE)
        self.lead = make_user("sharon", Role.PROGRAM_LEAD)
        self.clerk = make_user("chrisis", Role.SCHOOL_STAFF, school=self.school)

        self.shirt = self.priced_sku("White Shirt", "25000.00")

        self.url = reverse("dashboard:school")

    def priced_sku(self, name, price):
        garment = Garment.objects.create(name=name)
        GarmentPrice.objects.create(
            garment=garment, unit_price=Decimal(price), active_date=IN_FORCE
        )
        return Sku.objects.create(
            garment=garment,
            size=Size.objects.create(name=f"S{Size.objects.count() + 1}", sort_order=10),
        )

    def stock(self, quantity, sku=None):
        post_movement(
            warehouse=self.namayemba,
            sku=sku or self.shirt,
            quantity=quantity,
            movement_type=MovementType.RECEIPT,
            unit_value=Decimal("25000.00"),
            document_number="RC-100001",
            occurred_on=IN_FORCE,
            created_by=self.julius,
        )

    def order(self, quantity=2, school=None, student="Miriam Achieng"):
        return place_order(
            school=school or self.school,
            student_name=student,
            order_date=TODAY,
            skus=[{"sku": self.shirt, "quantity": quantity}],
            created_by=self.clerk,
        )

    def shipped_order(self, quantity=2):
        """An order taken all the way to Shipped, ready to be confirmed."""
        self.stock(quantity)
        order = self.order(quantity)
        release_order(order, released_by=self.finance)
        pick_order(order, picked_by=self.julius)
        ship_order(order, shipped_by=self.julius, shipped_on=TODAY)
        order.refresh_from_db()
        return order


class OnlyASchoolHasASchoolDashboard(SchoolDashboardSetup):
    """The two dashboards are two audiences, not one with a wider door."""

    def test_a_school_clerk_may_read_it(self):
        self.client.force_authenticate(self.clerk)

        self.assertEqual(self.client.get(self.url).status_code, status.HTTP_200_OK)

    def test_warehouse_staff_may_not(self):
        self.client.force_authenticate(self.julius)

        self.assertEqual(
            self.client.get(self.url).status_code, status.HTTP_403_FORBIDDEN
        )

    def test_the_leads_may_not(self):
        """Not a slight — there is nothing here for them.

        The view reads `request.user.school`, and a lead has none. Their view
        of a school's position is the order reports, which are scoped and
        already theirs to read.
        """
        self.client.force_authenticate(self.lead)

        self.assertEqual(
            self.client.get(self.url).status_code, status.HTTP_403_FORBIDDEN
        )

    def test_finance_may_not(self):
        self.client.force_authenticate(self.finance)

        self.assertEqual(
            self.client.get(self.url).status_code, status.HTTP_403_FORBIDDEN
        )

    def test_a_school_clerk_may_not_read_the_warehouse_dashboard(self):
        """The reason this endpoint exists at all."""
        self.client.force_authenticate(self.clerk)

        self.assertEqual(
            self.client.get(reverse("dashboard:summary")).status_code,
            status.HTTP_403_FORBIDDEN,
        )

    def test_a_school_account_with_no_school_is_told_so(self):
        """Rather than shown an empty dashboard that reads as "no orders"."""
        orphan = make_user("orphan", Role.SCHOOL_STAFF)
        self.client.force_authenticate(orphan)

        response = self.client.get(self.url)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("school", response.data)


class ItCountsThisSchoolsOrdersAndNobodyElses(SchoolDashboardSetup):
    def test_another_schools_orders_are_not_counted(self):
        """The one failure that would make the screen actively misleading."""
        other = School.objects.create(
            name="Bugiri Primary", primary_warehouse=self.namayemba
        )
        self.order(school=other, student="Someone Else")
        self.order()

        counts = services.school_order_counts(self.school)

        self.assertEqual(counts["total"], 1)

    def test_a_new_order_is_awaiting_payment(self):
        self.order()

        counts = services.school_order_counts(self.school)

        self.assertEqual(counts["awaiting_payment"], 1)
        self.assertEqual(counts["in_progress"], 0)

    def test_a_released_order_is_in_progress(self):
        order = self.order()
        release_order(order, released_by=self.finance)

        counts = services.school_order_counts(self.school)

        self.assertEqual(counts["awaiting_payment"], 0)
        self.assertEqual(counts["in_progress"], 1)

    def test_a_shipped_order_is_awaiting_confirmation_not_completed(self):
        """The distinction the whole completion step exists for.

        A parcel on a lorry is not a finished order. If this ever collapses
        into `completed`, a delivery that never arrives becomes invisible.
        """
        self.shipped_order()

        counts = services.school_order_counts(self.school)

        self.assertEqual(counts["awaiting_confirmation"], 1)
        self.assertEqual(counts["completed"], 0)

    def test_confirming_the_delivery_moves_it_to_completed(self):
        order = self.shipped_order()
        confirm_receipt(order.shipments.get(), confirmed_by=self.clerk)

        counts = services.school_order_counts(self.school)

        self.assertEqual(counts["awaiting_confirmation"], 0)
        self.assertEqual(counts["completed"], 1)


class ItSaysWhatTheSchoolOwes(SchoolDashboardSetup):
    def test_an_unpaid_order_is_outstanding(self):
        self.order(quantity=2)

        self.assertEqual(
            services.school_amount_outstanding(self.school), Decimal("50000.00")
        )

    def test_a_released_order_is_not_outstanding(self):
        """Releasing *is* the payment confirmation — there is no paid flag."""
        order = self.order(quantity=2)
        release_order(order, released_by=self.finance)

        self.assertEqual(
            services.school_amount_outstanding(self.school), Decimal("0.00")
        )

    def test_a_cancelled_order_is_not_a_debt(self):
        order = self.order(quantity=2)
        cancel_order(order, cancelled_by=self.clerk, reason="Parent withdrew")

        self.assertEqual(
            services.school_amount_outstanding(self.school), Decimal("0.00")
        )

    def test_a_school_with_no_orders_owes_nothing_rather_than_none(self):
        """A null here would render as an empty tile instead of UGX 0."""
        self.assertEqual(
            services.school_amount_outstanding(self.school), Decimal("0.00")
        )


class ItListsTheParcelsWorthChasing(SchoolDashboardSetup):
    def test_a_shipped_parcel_is_waiting_to_be_confirmed(self):
        order = self.shipped_order()

        rows = services.school_deliveries_to_confirm(self.school)

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["order_number"], order.number)
        self.assertEqual(rows[0]["from_warehouse"], self.namayemba.name)

    def test_a_confirmed_parcel_drops_off_the_list(self):
        order = self.shipped_order()
        confirm_receipt(order.shipments.get(), confirmed_by=self.clerk)

        self.assertEqual(services.school_deliveries_to_confirm(self.school), [])

    def test_another_schools_parcel_is_not_listed(self):
        other = School.objects.create(
            name="Bugiri Primary", primary_warehouse=self.namayemba
        )
        self.stock(2)
        theirs = place_order(
            school=other,
            student_name="Someone Else",
            order_date=TODAY,
            skus=[{"sku": self.shirt, "quantity": 2}],
            created_by=self.clerk,
        )
        release_order(theirs, released_by=self.finance)
        pick_order(theirs, picked_by=self.julius)
        ship_order(theirs, shipped_by=self.julius, shipped_on=TODAY)

        self.assertEqual(services.school_deliveries_to_confirm(self.school), [])


class TheEndpointReturnsWhatTheScreenDraws(SchoolDashboardSetup):
    def test_the_payload_carries_every_panel(self):
        self.shipped_order()
        self.client.force_authenticate(self.clerk)

        body = self.client.get(self.url).data

        self.assertEqual(body["school"]["name"], self.school.name)
        self.assertEqual(body["warehouse"]["name"], self.namayemba.name)
        self.assertEqual(body["orders"]["awaiting_confirmation"], 1)
        self.assertEqual(len(body["deliveries_to_confirm"]), 1)
        self.assertIn("backorders", body)
        self.assertIn("amount_outstanding", body)


class TheLeadHearsAboutALostParcel(SchoolDashboardSetup):
    """The alert the Shipped/Completed split exists for.

    Keeping the two apart makes a lost delivery visible — but only if
    somebody who can chase it is told. The school sees its own parcels, and
    the school is not who rings the warehouse.
    """

    def attention(self):
        from dashboard.services import needs_attention

        return {alert["kind"]: alert for alert in needs_attention()}

    def test_a_parcel_out_for_a_month_reaches_the_lead(self):
        from datetime import timedelta
        from django.utils import timezone

        order = self.shipped_order()
        shipment = order.shipments.get()
        shipment.shipped_on = timezone.localdate() - timedelta(days=30)
        shipment.save(update_fields=["shipped_on"])

        alert = self.attention().get("deliveries_unconfirmed")

        self.assertIsNotNone(alert)
        self.assertEqual(alert["count"], 1)
        self.assertIn("delivery", alert["message"])

    def test_a_parcel_that_left_this_morning_is_not_an_alert_yet(self):
        """Everything shipped today is unconfirmed and none of it is a
        problem."""
        self.shipped_order()

        self.assertNotIn("deliveries_unconfirmed", self.attention())

    def test_a_confirmed_parcel_stops_being_chased(self):
        from datetime import timedelta
        from django.utils import timezone

        order = self.shipped_order()
        shipment = order.shipments.get()
        shipment.shipped_on = timezone.localdate() - timedelta(days=30)
        shipment.save(update_fields=["shipped_on"])

        confirm_receipt(shipment, confirmed_by=self.clerk)

        self.assertNotIn("deliveries_unconfirmed", self.attention())
