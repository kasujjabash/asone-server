"""The ledger, in the admin. Read-only by design."""

from django.contrib import admin

from .models import ReasonCode, StockMovement


@admin.register(StockMovement)
class StockMovementAdmin(admin.ModelAdmin):
    """Append-only, so nothing here may add, change or delete.

    The model refuses those operations too — this only stops the admin
    offering buttons that would raise.
    """

    list_display = (
        "number",
        "occurred_on",
        "warehouse",
        "sku",
        "quantity",
        "movement_type",
        "document_number",
        "created_by",
    )
    list_filter = ("movement_type", "stock_status", "warehouse", "occurred_on")
    search_fields = ("number", "document_number", "sku__number", "sku__description")
    date_hierarchy = "occurred_on"

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(ReasonCode)
class ReasonCodeAdmin(admin.ModelAdmin):
    """Central Office maintains these. AsOne listed four and said "may be more"."""

    list_display = ("code", "name", "is_active")
    list_filter = ("is_active",)
    search_fields = ("code", "name")

    def has_delete_permission(self, request, obj=None):
        """Retire with `is_active` instead — past adjustments point at these."""
        return False
