"""Entry-point views.

Routing conveniences only — no business rules. These exist so that someone
who opens the server in a browser lands somewhere useful instead of a 404.
"""

from django.urls import reverse
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from drf_spectacular.utils import extend_schema


@extend_schema(exclude=True)
class ApiRootView(APIView):
    """A directory of what this API offers, at /api/.

    Open to anyone: it lists paths, never data. Knowing that a login endpoint
    exists tells an attacker nothing they could not learn by trying it.
    """

    permission_classes = [AllowAny]

    def get(self, request):
        return Response(
            {
                "name": "AsOne Logistics API",
                "documentation": request.build_absolute_uri(reverse("swagger-ui")),
                "schema": request.build_absolute_uri(reverse("schema")),
                "authentication": {
                    "login": request.build_absolute_uri(reverse("accounts:login")),
                    "refresh": request.build_absolute_uri(reverse("accounts:refresh")),
                    "verify": request.build_absolute_uri(reverse("accounts:verify")),
                    "logout": request.build_absolute_uri(reverse("accounts:logout")),
                    "me": request.build_absolute_uri(reverse("accounts:me")),
                    "password_change": request.build_absolute_uri(
                        reverse("accounts:password-change")
                    ),
                },
                "master_data": {
                    "garments": request.build_absolute_uri(reverse("catalog:garment-list")),
                    "skus": request.build_absolute_uri(reverse("catalog:sku-list")),
                    "prices": request.build_absolute_uri(reverse("catalog:garment-price-list")),
                    "price_lists": request.build_absolute_uri(reverse("catalog:price-list")),
                    "sizes": request.build_absolute_uri(reverse("catalog:size-list")),
                    "minimum_stock_levels": request.build_absolute_uri(
                        reverse("catalog:minimum-stock-level-list")
                    ),
                    "tailoring_centers": request.build_absolute_uri(
                        reverse("catalog:tailoring-center-list")
                    ),
                    "warehouses": request.build_absolute_uri(reverse("catalog:warehouse-list")),
                    "schools": request.build_absolute_uri(reverse("catalog:school-list")),
                },
                # Filled in as each app gains an API.
                "not_yet_built": ["inventory", "procurement", "orders"],
            }
        )
