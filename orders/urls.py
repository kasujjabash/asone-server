"""School order routes, mounted at /api/orders/ by config/urls.py."""

from django.urls import include, path
from rest_framework.routers import DefaultRouter

from . import views

app_name = "orders"

router = DefaultRouter()
router.register("school-orders", views.SchoolOrderViewSet, "school-order")

urlpatterns = [
    path("reports/on-hold/", views.OrdersOnHoldView.as_view(), name="orders-on-hold"),
    path("", include(router.urls)),
]
