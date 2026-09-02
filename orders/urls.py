"""School order routes, mounted at /api/orders/ by config/urls.py."""

from django.urls import include, path
from rest_framework.routers import DefaultRouter

from . import views

app_name = "orders"

router = DefaultRouter()
router.register("school-orders", views.SchoolOrderViewSet, "school-order")
router.register("backorders", views.BackorderViewSet, "backorder")

urlpatterns = [
    path("reports/on-hold/", views.OrdersOnHoldView.as_view(), name="orders-on-hold"),
    path(
        "reports/backorders/",
        views.OutstandingBackordersView.as_view(),
        name="backorders-outstanding",
    ),
    path(
        "reports/part-processed/",
        views.PartProcessedOrdersView.as_view(),
        name="orders-part-processed",
    ),
    path(
        "reports/shipments-costed/",
        views.CostedShipmentsView.as_view(),
        name="shipments-costed",
    ),
    path("", include(router.urls)),
]
