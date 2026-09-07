"""Shapes for the dashboard. None of these are models — every figure is
computed, so they are all plain Serializers."""

from rest_framework import serializers


class DashboardSummarySerializer(serializers.Serializer):
    """The six tiles across the top — F62."""

    available_units = serializers.IntegerField(
        help_text="Units on hand and free to promise."
    )
    inventory_value = serializers.DecimalField(
        max_digits=18,
        decimal_places=2,
        help_text="What that stock is carried at, in UGX.",
    )
    units_awaiting_pick = serializers.IntegerField(
        help_text="Garments on orders the warehouse has not picked yet."
    )
    orders_awaiting_dispatch = serializers.IntegerField(
        help_text="Orders picked but not yet shipped — off the shelf, still in the building."
    )
    outstanding_backorders = serializers.IntegerField(
        help_text="Backorders open or assigned, not yet shipped."
    )
    skus_below_minimum = serializers.IntegerField(
        help_text="SKUs at or under their reorder floor."
    )


class AttentionAlertSerializer(serializers.Serializer):
    """One row of the "Needs Attention" list.

    A count and a sentence, not the underlying records — the row links
    through to the screen that has those.
    """

    kind = serializers.CharField(
        help_text="Stable identifier for the frontend to route on: "
        "low_stock, orders_on_hold, receipts_unreconciled, backorders_fillable."
    )
    level = serializers.CharField(help_text="CRITICAL, HOLD, INSPECTION or READY.")
    count = serializers.IntegerField()
    message = serializers.CharField()


class ActivityEventSerializer(serializers.Serializer):
    """One line of the recent activity timeline."""

    at = serializers.DateTimeField()
    kind = serializers.CharField(
        help_text="receipt, shipment, adjustment, production_order or backorder."
    )
    reference = serializers.CharField(help_text="The document number, for linking.")
    description = serializers.CharField()


class OrderVolumeDaySerializer(serializers.Serializer):
    date = serializers.DateField()
    orders = serializers.IntegerField()


class OrderVolumeSerializer(serializers.Serializer):
    """The daily orders chart.

    Days with no orders are absent rather than zero — see
    `daily_order_volume()` for why that is the caller's decision.
    """

    days = OrderVolumeDaySerializer(many=True)
    total = serializers.IntegerField()
    average_per_day = serializers.FloatField()


class NotificationSerializer(serializers.Serializer):
    """One item behind the bell. Same shape as an attention alert, because
    it is the same condition seen from the header rather than the panel."""

    kind = serializers.CharField()
    level = serializers.CharField()
    message = serializers.CharField()
    count = serializers.IntegerField()


class NotificationsSerializer(serializers.Serializer):
    """The bell: a badge count and the list behind it.

    `unread_count` is the number of *conditions* currently true, not a
    per-user inbox — reading them does not clear it. See
    `dashboard/services.py::notifications` for why, and for what a real
    inbox would need.
    """

    unread_count = serializers.IntegerField(
        help_text="Badge number. Falls when the underlying problem is fixed, not when read."
    )
    notifications = NotificationSerializer(many=True)


class WarehouseInventorySerializer(serializers.Serializer):
    """One site's line in the "Inventory by Warehouse" panel."""

    warehouse_id = serializers.IntegerField()
    warehouse_name = serializers.CharField()
    units = serializers.IntegerField()
    value = serializers.DecimalField(max_digits=18, decimal_places=2)
    sku_count = serializers.IntegerField(
        help_text="Distinct SKUs actually held here — not the catalogue size."
    )


class InventoryByWarehouseSerializer(serializers.Serializer):
    """The panel. The bar is proportional and the frontend scales it —
    returning a percentage would bake in whether the scale is against the
    largest site or the total, which is a design decision."""

    warehouses = WarehouseInventorySerializer(many=True)
    total_units = serializers.IntegerField()
    total_value = serializers.DecimalField(max_digits=18, decimal_places=2)
    total_skus = serializers.IntegerField(
        help_text="Distinct SKUs held anywhere, counted once — not the sum of the per-site counts."
    )


class WeeklyReportRowSerializer(serializers.Serializer):
    """One SKU at one warehouse, over the week.

    Opening plus the movements equals closing, on every row.
    """

    warehouse = serializers.CharField()
    sku_number = serializers.CharField()
    description = serializers.CharField()
    opening = serializers.IntegerField()
    received = serializers.IntegerField()
    adjusted = serializers.IntegerField(help_text="Signed — corrections, damages, losses.")
    transferred = serializers.IntegerField(help_text="Signed — net of in and out.")
    picked = serializers.IntegerField()
    shipped = serializers.IntegerField()
    returned = serializers.IntegerField()
    closing = serializers.IntegerField()
    value = serializers.DecimalField(max_digits=18, decimal_places=2)


class WeeklyReportSerializer(serializers.Serializer):
    """The weekly inventory report, as data. `/download/` is the same thing
    as a spreadsheet."""

    date_from = serializers.DateField()
    date_to = serializers.DateField()
    rows = WeeklyReportRowSerializer(many=True)
    total_closing_units = serializers.IntegerField()
    total_closing_value = serializers.DecimalField(max_digits=18, decimal_places=2)
