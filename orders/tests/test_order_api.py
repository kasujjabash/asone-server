"""The point of sale over HTTP.

Two things this proves that the service tests cannot:

    a school clerk can only ever order for their own school
    nobody else can reach the point of sale at all

The second is the matrix: AsOne leaves the School Orders Entry column blank
for both leads, and the Role Access sheet omits the point of sale from their
screens.
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

IN_FORCE = date(2026, 1, 1)
ORDERED_ON = date(2026, 11, 10)
Role = User.Role
Level = Garment.SchoolLevel


class OrderApiSetup(APITestCase):
    def setUp(self):
        self.sites = build_sites()
        self.school = self.sites["school_a"]       # Namayemba PS, Primary
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

    def place(self, user=None, **overrides):
        self.client.force_authenticate(user or self.chrisis)
        payload = {
            "student_name": "Miriam Achieng",
            "order_date": ORDERED_ON.isoformat(),
            "skus": [{"sku": self.shirt.pk, "quantity": 2}],
        }
        payload.update(overrides)
        return self.client.post(
            reverse("orders:school-order-list"), payload, format="json"
        )


class OnlySchoolStaffReachThePointOfSale(OrderApiSetup):
    """The matrix leaves School Orders Entry blank for every other role."""

    def test_a_school_clerk_may_use_it(self):
        self.client.force_authenticate(self.chrisis)
        self.assertEqual(
            self.client.get(reverse("orders:school-order-list")).status_code,
            status.HTTP_200_OK,
        )

    def test_the_leads_may_not(self):
        """Deliberate, and easy to assume otherwise — the leads see almost
        everything else."""
        for user in (self.lead, make_user("andrew", Role.OPERATIONS_MANAGER)):
            with self.subTest(user=user.email):
                self.client.force_authenticate(user)
                self.assertEqual(
                    self.client.get(reverse("orders:school-order-list")).status_code,
                    status.HTTP_403_FORBIDDEN,
                )

    def test_warehouse_staff_may_not(self):
        self.client.force_authenticate(self.clerk)

        self.assertEqual(
            self.client.get(reverse("orders:school-order-list")).status_code,
            status.HTTP_403_FORBIDDEN,
        )

    def test_finance_may_read_but_not_place(self):
        """F34 gives Finance a view of the invoice — reading is not acting.

        Narrower than it looks: they can see an order, and can neither place
        nor cancel one.
        """
        self.client.force_authenticate(self.finance)

        self.assertEqual(
            self.client.get(reverse("orders:school-order-list")).status_code,
            status.HTTP_200_OK,
        )
        self.assertEqual(self.place(user=self.finance).status_code, status.HTTP_403_FORBIDDEN)


class ASchoolOrdersOnlyForItself(OrderApiSetup):
    def test_the_order_belongs_to_the_clerks_school(self):
        response = self.place()

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data["school"], self.school.pk)

    def test_naming_another_school_in_the_body_changes_nothing(self):
        """`school` is not a writable field — sending one is ignored."""
        response = self.place(school=self.other_school.pk)

        self.assertEqual(response.data["school"], self.school.pk)

    def test_a_clerk_cannot_see_another_schools_orders(self):
        self.place(user=self.chrisis)

        self.client.force_authenticate(self.peter)
        listed = self.client.get(reverse("orders:school-order-list")).data

        self.assertEqual(listed["count"], 0)

    def test_a_clerk_cannot_open_another_schools_order_directly(self):
        created = self.place(user=self.chrisis)

        self.client.force_authenticate(self.peter)
        response = self.client.get(
            reverse("orders:school-order-detail", args=[created.data["id"]])
        )
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)


class PlacingAnOrderOverHttp(OrderApiSetup):
    def test_an_order_comes_back_on_hold_with_a_number(self):
        response = self.place()

        self.assertEqual(response.data["status"], OrderStatus.HOLD)
        self.assertTrue(response.data["number"].startswith("SO-"))

    def test_a_kit_comes_back_exploded_into_skus(self):
        response = self.place(
            skus=[], kits=[{"kit": self.kit.pk, "quantity": 1}]
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        lines = response.data["lines"]
        self.assertEqual(len(lines), 2)
        self.assertTrue(all(line["from_kit"] == self.kit.pk for line in lines))

    def test_the_total_is_returned(self):
        response = self.place(
            skus=[], kits=[{"kit": self.kit.pk, "quantity": 1}]
        )

        # 2 shirts @ 25000 + 1 tunic @ 30000
        self.assertEqual(Decimal(response.data["total"]), Decimal("80000.00"))

    def test_kits_and_items_can_be_ordered_together(self):
        response = self.place(
            kits=[{"kit": self.kit.pk, "quantity": 1}],
            skus=[{"sku": self.shirt.pk, "quantity": 1}],
        )

        self.assertEqual(len(response.data["lines"]), 3)

    def test_the_demand_view_adds_the_shirts_back_together(self):
        created = self.place(
            kits=[{"kit": self.kit.pk, "quantity": 1}],
            skus=[{"sku": self.shirt.pk, "quantity": 1}],
        )

        rows = self.client.get(
            reverse("orders:school-order-demand", args=[created.data["id"]])
        ).data
        by_sku = {row["sku_number"]: row["quantity"] for row in rows}

        self.assertEqual(by_sku[self.shirt.number], 3)
        self.assertEqual(by_sku[self.tunic.number], 1)

    def test_an_empty_order_is_a_400(self):
        response = self.place(skus=[], kits=[])

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_a_blank_student_name_is_a_400(self):
        response = self.place(student_name="   ")

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_ordering_the_wrong_school_level_is_a_400(self):
        """A Primary school ordering a High School garment."""
        blazer = self.priced_sku("HS Blazer", Level.HIGH, "60000.00")

        response = self.place(skus=[{"sku": blazer.pk, "quantity": 1}])
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_ordering_a_retired_item_is_a_400(self):
        self.shirt.is_active = False
        self.shirt.save(update_fields=["is_active"])

        self.assertEqual(self.place().status_code, status.HTTP_400_BAD_REQUEST)

    def test_an_order_cannot_be_deleted(self):
        """The number is an invoice a parent is holding."""
        created = self.place()

        response = self.client.delete(
            reverse("orders:school-order-detail", args=[created.data["id"]])
        )
        self.assertEqual(response.status_code, status.HTTP_405_METHOD_NOT_ALLOWED)

    def test_status_cannot_be_set_from_the_request(self):
        """Releasing an order needs payment confirmed — open question Q2."""
        created = self.place()

        response = self.client.patch(
            reverse("orders:school-order-detail", args=[created.data["id"]]),
            {"status": OrderStatus.RELEASED},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        order = SchoolOrder.objects.get(pk=created.data["id"])
        self.assertEqual(order.status, OrderStatus.HOLD)
