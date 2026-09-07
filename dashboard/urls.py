"""Dashboard routes, mounted at /api/dashboard/ by config/urls.py."""

from django.urls import path

from . import views

app_name = "dashboard"

urlpatterns = [
    path("summary/", views.SummaryView.as_view(), name="summary"),
    path("attention/", views.AttentionView.as_view(), name="attention"),
    path("activity/", views.ActivityView.as_view(), name="activity"),
    path("order-volume/", views.OrderVolumeView.as_view(), name="order-volume"),
    path("notifications/", views.NotificationsView.as_view(), name="notifications"),
    path(
        "inventory-by-warehouse/",
        views.InventoryByWarehouseView.as_view(),
        name="inventory-by-warehouse",
    ),
    path("weekly-report/", views.WeeklyReportView.as_view(), name="weekly-report"),
    path(
        "weekly-report/download/",
        views.WeeklyReportDownloadView.as_view(),
        name="weekly-report-download",
    ),
]
