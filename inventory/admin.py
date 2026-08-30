"""The ledger, in the admin. Read-only by design."""

from django.contrib import admin

from .models import (
    InventoryAdjustment,
    ReasonCode,
    StockMovement,
    WarehouseTransfer,
    WarehouseTransferLine,
)


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


class WarehouseTransferLineInline(admin.TabularInline):
    model = WarehouseTransferLine
    extra = 0
    fields = ("sku", "quantity", "unit_value")
    readonly_fields = ("unit_value",)


@admin.register(WarehouseTransfer)
class WarehouseTransferAdmin(admin.ModelAdmin):
    """Stock rebalanced between the two warehouses — F25.

    Posting is deliberately not offered here. It writes two permanent ledger
    rows and re-checks stock, so it goes through the API where those failures
    can be reported properly.
    """

    list_display = ("number", "transfer_date", "from_warehouse", "to_warehouse", "is_posted")
    list_filter = ("from_warehouse", "to_warehouse", "transfer_date")
    search_fields = ("number", "notes")
    date_hierarchy = "transfer_date"
    readonly_fields = ("number", "posted_at", "created_by", "created_at")
    inlines = [WarehouseTransferLineInline]

    @admin.display(boolean=True, description="Posted")
    def is_posted(self, obj):
        return obj.is_posted

    def has_delete_permission(self, request, obj=None):
        """A posted transfer is the source of ledger rows; an unposted one
        still records that somebody intended to move stock."""
        return False


@admin.register(InventoryAdjustment)
class InventoryAdjustmentAdmin(admin.ModelAdmin):
    """A quantity change against a reason code — F23.

    Posting is deliberately not offered here, same reasoning as
    WarehouseTransferAdmin: it writes a permanent ledger row and needs its
    failures — an already-posted adjustment, a SKU that lost its price —
    reported properly through the API, not swallowed by the admin.
    """

    list_display = (
        "number",
        "adjustment_date",
        "warehouse",
        "sku",
        "quantity",
        "reason_code",
        "is_posted",
    )
    list_filter = ("warehouse", "reason_code", "adjustment_date")
    search_fields = ("number", "sku__number", "sku__description", "notes")
    date_hierarchy = "adjustment_date"
    readonly_fields = ("number", "unit_value", "posted_at", "created_by", "created_at")
    autocomplete_fields = ("sku",)

    @admin.display(boolean=True, description="Posted")
    def is_posted(self, obj):
        return obj.is_posted

    def has_delete_permission(self, request, obj=None):
        """An unposted adjustment still records intent; a posted one is the
        source of a ledger row."""
        return False
