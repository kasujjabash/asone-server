"""Central Office screens for the site tables.

These are the master-data screens AsOne's central team uses. Whether an
otherwise-standard Django admin is acceptable for this, or whether every
screen must carry AsOne branding, is open question Q8 — and the answer moves
the timeline, because branded screens mean building them in React instead of
getting them free here.
"""

from django.contrib import admin

from .models import School, TailoringCenter, Warehouse


@admin.register(TailoringCenter)
class TailoringCenterAdmin(admin.ModelAdmin):
    list_display = ("name", "address")
    search_fields = ("name",)


@admin.register(Warehouse)
class WarehouseAdmin(admin.ModelAdmin):
    list_display = ("name", "primary_tailoring_center")
    list_filter = ("primary_tailoring_center",)
    search_fields = ("name",)


@admin.register(School)
class SchoolAdmin(admin.ModelAdmin):
    list_display = ("name", "level", "primary_warehouse")
    list_filter = ("level", "primary_warehouse")
    search_fields = ("name",)
