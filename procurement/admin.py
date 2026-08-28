"""Central Office screens for group and production orders."""

from django.contrib import admin

from .models import GroupOrder, GroupOrderLine, ProductionOrder, ProductionOrderLine


class OrderLineInline(admin.TabularInline):
    """Lines belong on the order. Seen apart, a line is just a number."""

    extra = 0
    autocomplete_fields = ("sku",)
    fields = ("sku", "quantity", "unit_price")


class GroupOrderLineInline(OrderLineInline):
    model = GroupOrderLine


class ProductionOrderLineInline(OrderLineInline):
    model = ProductionOrderLine


class OrderAdmin(admin.ModelAdmin):
    """Orders are never deleted.

    A group order funds the Tailoring Centers and a production order is a
    commitment to one, so a withdrawn order is cancelled rather than erased —
    the document and its number have to survive.
    """

    readonly_fields = ("number", "created_by", "created_at")
    date_hierarchy = "order_date"

    def has_delete_permission(self, request, obj=None):
        return False

    def save_model(self, request, obj, form, change):
        # Every transaction records the user (p.9). Taken from the request,
        # never from a form field.
        if not change:
            obj.created_by = request.user
        super().save_model(request, obj, form, change)


@admin.register(GroupOrder)
class GroupOrderAdmin(OrderAdmin):
    list_display = ("number", "order_date", "due_in_warehouse_date", "status", "created_by")
    list_filter = ("status", "order_date")
    search_fields = ("number", "notes")
    inlines = [GroupOrderLineInline]


@admin.register(ProductionOrder)
class ProductionOrderAdmin(OrderAdmin):
    list_display = (
        "number",
        "order_date",
        "tailoring_center",
        "warehouse",
        "status",
        "group_order",
    )
    list_filter = ("status", "tailoring_center", "warehouse")
    search_fields = ("number", "notes")
    autocomplete_fields = ("group_order",)
    inlines = [ProductionOrderLineInline]
