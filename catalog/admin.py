"""Central Office screens for master data.

Whether an otherwise-standard Django admin is acceptable here, or whether
every screen must carry AsOne branding, is an open question with the client —
and the answer moves the timeline, because branded screens mean building them
in React instead of getting them free here.
"""

from django.contrib import admin

from .models import (
    Garment,
    GarmentPrice,
    Kit,
    KitItem,
    MinimumStockLevel,
    School,
    Size,
    Sku,
    TailoringCenter,
    Warehouse,
)
from .services import EmptyKit, PriceNotSet, compute_kit_price, price_for


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


@admin.register(Size)
class SizeAdmin(admin.ModelAdmin):
    list_display = ("name", "sort_order")
    list_editable = ("sort_order",)


class GarmentPriceInline(admin.TabularInline):
    """Price history, shown on the garment it belongs to.

    Deliberately an inline. Prices are meaningless apart from their garment,
    and seeing the whole history together is what stops someone editing an old
    row when they meant to add a new one.
    """

    model = GarmentPrice
    extra = 0
    ordering = ("-active_date",)
    fields = ("unit_price", "active_date", "expiration_date")


@admin.register(Garment)
class GarmentAdmin(admin.ModelAdmin):
    list_display = ("name", "school_level", "colour", "current_price", "is_active")
    list_filter = ("school_level", "is_active")
    search_fields = ("name", "colour")
    inlines = [GarmentPriceInline]

    @admin.display(description="Price today")
    def current_price(self, obj):
        """Today's price, or a visible gap.

        An unpriced garment cannot appear on a price list, so it is worth
        seeing at a glance rather than discovering when a school cannot order.
        """
        try:
            return price_for(obj)
        except PriceNotSet:
            return "— not priced —"


@admin.register(GarmentPrice)
class GarmentPriceAdmin(admin.ModelAdmin):
    """The full pricing table, for auditing across garments.

    Day-to-day editing happens on the garment, through the inline above.
    """

    list_display = ("garment", "unit_price", "active_date", "expiration_date")
    list_filter = ("garment__school_level", "active_date")
    search_fields = ("garment__name",)
    date_hierarchy = "active_date"
    autocomplete_fields = ("garment",)


class MinimumStockLevelInline(admin.TabularInline):
    """Per-warehouse reorder floors, shown on the SKU they belong to."""

    model = MinimumStockLevel
    extra = 0
    fields = ("warehouse", "minimum_quantity")


@admin.register(Sku)
class SkuAdmin(admin.ModelAdmin):
    """SKUs.

    The control number and description are read-only. The number is assigned
    by the system and printed on documents; the description is derived from
    the garment and size, so editing it here would let the two disagree.
    """

    list_display = ("number", "description", "garment", "size", "unit_price_today", "is_active")
    list_filter = ("is_active", "garment__school_level", "size")
    search_fields = ("number", "description", "garment__name")
    readonly_fields = ("number", "description")
    autocomplete_fields = ("garment",)
    inlines = [MinimumStockLevelInline]

    @admin.display(description="Price today")
    def unit_price_today(self, obj):
        """Read through to the garment. SKUs carry no price of their own."""
        try:
            return price_for(obj.garment)
        except PriceNotSet:
            return "— not priced —"


@admin.register(MinimumStockLevel)
class MinimumStockLevelAdmin(admin.ModelAdmin):
    """The full reorder-floor table, for auditing across warehouses."""

    list_display = ("sku", "warehouse", "minimum_quantity")
    list_filter = ("warehouse",)
    search_fields = ("sku__number", "sku__description")
    list_editable = ("minimum_quantity",)
    autocomplete_fields = ("sku",)


class KitItemInline(admin.TabularInline):
    """The kit's bill of materials, shown on the kit it belongs to.

    Deliberately an inline, same reasoning as GarmentPriceInline: a line item
    is meaningless apart from the kit it belongs to, and seeing the whole
    bill of materials together is what stops someone adding a duplicate line
    for a SKU that is already on it.
    """

    model = KitItem
    extra = 0
    fields = ("sku", "quantity")
    autocomplete_fields = ("sku",)


@admin.register(Kit)
class KitAdmin(admin.ModelAdmin):
    """Uniform Kits — bundles of SKUs sold as one unit."""

    list_display = ("kit_number", "name", "school_level", "current_price", "is_active")
    list_filter = ("school_level", "is_active")
    search_fields = ("kit_number", "name")
    inlines = [KitItemInline]

    @admin.display(description="Price today")
    def current_price(self, obj):
        """Today's kit price, or a visible gap.

        Mirrors GarmentAdmin.current_price: a kit that cannot be priced
        cannot be sold, so that is worth seeing at a glance in the list
        rather than discovering it later.
        """
        try:
            return compute_kit_price(obj)
        except (PriceNotSet, EmptyKit):
            return "— not priced —"
