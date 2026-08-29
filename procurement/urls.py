"""Procurement routes, mounted at /api/procurement/ by config/urls.py."""

from django.urls import include, path
from rest_framework.routers import DefaultRouter

from . import views

app_name = "procurement"

router = DefaultRouter()
router.register("group-orders", views.GroupOrderViewSet, "group-order")
router.register("production-orders", views.ProductionOrderViewSet, "production-order")
router.register("receipts", views.ReceiptViewSet, "receipt")

urlpatterns = [
    # Finance reports
    path(
        "reports/group-orders-costed/",
        views.GroupOrdersCostedView.as_view(),
        name="group-orders-costed",
    ),
    path(
        "reports/receipts-costed/",
        views.ReceiptsCostedView.as_view(),
        name="receipts-costed",
    ),
    path(
        "production-orders/open/",
        views.OpenProductionOrderView.as_view(),
        name="open-production-orders",
    ),
    path(
        "production-orders/<int:pk>/outstanding/",
        views.OutstandingOnOrderView.as_view(),
        name="outstanding-on-order",
    ),
    path("", include(router.urls)),
]
