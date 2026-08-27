"""Master data routes, mounted at /api/catalog/ by config/urls.py."""

from django.urls import include, path
from rest_framework.routers import DefaultRouter

from . import views

app_name = "catalog"

router = DefaultRouter()
# Sites
router.register("tailoring-centers", views.TailoringCenterViewSet, "tailoring-center")
router.register("warehouses", views.WarehouseViewSet, "warehouse")
router.register("schools", views.SchoolViewSet, "school")
# Products
router.register("garments", views.GarmentViewSet, "garment")
router.register("sizes", views.SizeViewSet, "size")
router.register("skus", views.SkuViewSet, "sku")
router.register("minimum-stock-levels", views.MinimumStockLevelViewSet, "minimum-stock-level")
# Pricing
# Basename "garment-price", not "price": a router basename of "price" would
# generate a route named "price-list" for the *table*, colliding with the
# PS/HS price-list endpoint below, which is a different thing entirely.
router.register("prices", views.GarmentPriceViewSet, "garment-price")
# Kits
router.register("kits", views.KitViewSet, "kit")
router.register("kit-items", views.KitItemViewSet, "kit-item")

urlpatterns = [
    path("price-lists/", views.PriceListView.as_view(), name="price-list"),
    path("price-lists/gaps/", views.PriceGapView.as_view(), name="price-gaps"),
    path("", include(router.urls)),
]
