"""Inventory routes, mounted at /api/inventory/ by config/urls.py."""

from django.urls import include, path
from rest_framework.routers import DefaultRouter

from . import views

app_name = "inventory"

router = DefaultRouter()
router.register("movements", views.StockMovementViewSet, "movement")
router.register("reason-codes", views.ReasonCodeViewSet, "reason-code")
router.register("transfers", views.WarehouseTransferViewSet, "transfer")
router.register("adjustments", views.InventoryAdjustmentViewSet, "adjustment")

urlpatterns = [
    path("stock-levels/", views.StockLevelView.as_view(), name="stock-levels"),
    path("reorder-alerts/", views.ReorderAlertView.as_view(), name="reorder-alerts"),
    path(
        "reports/adjustments-costed/",
        views.CostedAdjustmentsView.as_view(),
        name="adjustments-costed",
    ),
    path("", include(router.urls)),
]
