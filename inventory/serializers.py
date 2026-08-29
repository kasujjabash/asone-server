"""Serializers for the stock ledger.

Everything here is read-only. The ledger is written by services — a receipt
being posted, later an adjustment — never by a client sending a movement.
"""

from config.validators import CaseInsensitiveUniqueValidator
from rest_framework import serializers

from .models import ReasonCode, StockMovement


class StockMovementSerializer(serializers.ModelSerializer):
    """One ledger row. The audit trail (F48) is a list of these."""

    sku_number = serializers.CharField(source="sku.number", read_only=True)
    sku_description = serializers.CharField(source="sku.description", read_only=True)
    warehouse_name = serializers.CharField(source="warehouse.name", read_only=True)
    movement_type_display = serializers.CharField(
        source="get_movement_type_display", read_only=True
    )
    created_by_name = serializers.CharField(
        source="created_by.get_full_name", read_only=True
    )
    total_value = serializers.DecimalField(
        max_digits=16, decimal_places=2, read_only=True
    )

    class Meta:
        model = StockMovement
        fields = (
            "id",
            "number",
            "occurred_on",
            "warehouse",
            "warehouse_name",
            "sku",
            "sku_number",
            "sku_description",
            "quantity",
            "movement_type",
            "movement_type_display",
            "stock_status",
            "unit_value",
            "total_value",
            "document_number",
            "source",
            "destination",
            "created_by",
            "created_by_name",
            "created_at",
        )
        read_only_fields = fields


class StockLevelSerializer(serializers.Serializer):
    """A derived stock level — F47.

    Not a model. There is no stock-level table: this is a `Sum()` over the
    ledger, computed on read.
    """

    sku_id = serializers.IntegerField()
    sku_number = serializers.CharField(source="sku__number")
    sku_description = serializers.CharField(source="sku__description")
    warehouse_id = serializers.IntegerField()
    warehouse_name = serializers.CharField(source="warehouse__name")
    level = serializers.IntegerField(help_text="Units on hand.")
    value = serializers.DecimalField(
        max_digits=16, decimal_places=2, help_text="Value of that stock."
    )


class ReorderAlertSerializer(serializers.Serializer):
    """A SKU at or below its reorder floor — F50."""

    sku_number = serializers.CharField(source="sku.number")
    sku_description = serializers.CharField(source="sku.description")
    warehouse_name = serializers.CharField(source="warehouse.name")
    level = serializers.IntegerField()
    minimum = serializers.IntegerField()
    shortfall = serializers.IntegerField(
        help_text="How far below the floor. Zero means exactly at it."
    )


class ReasonCodeSerializer(serializers.ModelSerializer):
    """An inventory adjustment reason code — F13.

    Writable, unlike everything else in this app. The ledger is written by
    services; this is master data Central Office maintains, and AsOne said
    there would be more codes than the four they listed.
    """

    code = serializers.CharField(
        max_length=20,
        validators=[CaseInsensitiveUniqueValidator(queryset=ReasonCode.objects.all())],
    )
    name = serializers.CharField(
        max_length=120,
        validators=[CaseInsensitiveUniqueValidator(queryset=ReasonCode.objects.all())],
    )

    class Meta:
        model = ReasonCode
        fields = ("id", "code", "name", "description", "is_active")
