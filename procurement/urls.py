"""Procurement routes, mounted at /api/procurement/ by config/urls.py."""

from django.urls import include, path
from rest_framework.routers import DefaultRouter

from . import views

app_name = "procurement"

router = DefaultRouter()
router.register("group-orders", views.GroupOrderViewSet, "group-order")
router.register("production-orders", views.ProductionOrderViewSet, "production-order")

urlpatterns = [
    path(
        "production-orders/open/",
        views.OpenProductionOrderView.as_view(),
        name="open-production-orders",
    ),
    path("", include(router.urls)),
]
