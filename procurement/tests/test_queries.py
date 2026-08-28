"""Query counts on the procurement listings.

Orders carry nested lines, which is exactly the shape that produces an N+1:
one query for the orders, then one per order for its lines, then one per line
for the SKU. On rural connections that drop, that is the difference between a
page loading and a page timing out.
"""

from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from rest_framework.test import APITestCase

from accounts.models import User
from accounts.tests.factories import build_sites, make_user
from catalog.models import TailoringCenter
from procurement.services import create_group_order, create_production_order

from .factories import ORDER_DATE, make_priced_sku


class QueryCountTests(APITestCase):
    def setUp(self):
        self.sites = build_sites()
        self.lead = make_user("sharon", User.Role.PROGRAM_LEAD)
        self.client.force_authenticate(self.lead)
        self.tc = TailoringCenter.objects.create(name="Idudi TC")

        self.skus = [
            make_priced_sku("White Shirt", "10"),
            make_priced_sku("Grey Trousers", "12"),
            make_priced_sku("Jumper", "14"),
        ]
        lines = [{"sku": sku, "quantity": 100} for sku in self.skus]

        for _ in range(10):
            create_group_order(
                created_by=self.lead, lines=lines, order_date=ORDER_DATE
            )
            create_production_order(
                created_by=self.lead,
                lines=lines,
                order_date=ORDER_DATE,
                tailoring_center=self.tc,
                warehouse=self.sites["namayemba"],
            )

    def assert_flat(self, route, limit=8):
        with CaptureQueriesContext(connection) as queries:
            response = self.client.get(reverse(route))

        self.assertEqual(response.status_code, 200)
        self.assertLessEqual(
            len(queries),
            limit,
            msg=f"{route} ran {len(queries)} queries for 10 orders of 3 lines — "
            "lines or SKUs are being fetched per row.",
        )

    def test_listing_group_orders_does_not_scale_with_lines(self):
        self.assert_flat("procurement:group-order-list")

    def test_listing_production_orders_does_not_scale_with_lines(self):
        self.assert_flat("procurement:production-order-list")

    def test_the_open_orders_view_does_not_scale_with_lines(self):
        self.assert_flat("procurement:open-production-orders")
