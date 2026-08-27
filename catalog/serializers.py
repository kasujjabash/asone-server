"""Serializers for master data.

Shape only — which fields may be read, which may be written. Business logic
lives in catalog/services.py.

Money is serialised as a string, not a float. `2500.10` cannot be represented
exactly in binary floating point, and a price that arrives at the frontend as
2500.099999 becomes a wrong invoice. DRF does this by default for
DecimalField; it is spelled out here so nobody "fixes" it later.
"""

import copy
from decimal import Decimal

from rest_framework import serializers

from .models import (
    Garment,
    GarmentPrice,
    MinimumStockLevel,
    School,
    Size,
    Sku,
    TailoringCenter,
    Warehouse,
)
from .services import CURRENT_PRICE_ANNOTATION, PriceNotSet, price_for


# ---------------------------------------------------------------------------
# Sites
# ---------------------------------------------------------------------------


class TailoringCenterSerializer(serializers.ModelSerializer):
    class Meta:
        model = TailoringCenter
        fields = ("id", "name", "address")


class WarehouseSerializer(serializers.ModelSerializer):
    primary_tailoring_center_name = serializers.CharField(
        source="primary_tailoring_center.name", read_only=True, default=None
    )

    class Meta:
        model = Warehouse
        fields = (
            "id",
            "name",
            "address",
            "primary_tailoring_center",
            "primary_tailoring_center_name",
        )


class SchoolSerializer(serializers.ModelSerializer):
    level_display = serializers.CharField(source="get_level_display", read_only=True)
    primary_warehouse_name = serializers.CharField(
        source="primary_warehouse.name", read_only=True
    )

    class Meta:
        model = School
        fields = (
            "id",
            "name",
            "level",
            "level_display",
            "address",
            "primary_warehouse",
            "primary_warehouse_name",
        )


# ---------------------------------------------------------------------------
# Products
# ---------------------------------------------------------------------------


class SizeSerializer(serializers.ModelSerializer):
    class Meta:
        model = Size
        fields = ("id", "name", "sort_order")


class GarmentSerializer(serializers.ModelSerializer):
    school_level_display = serializers.CharField(
        source="get_school_level_display", read_only=True
    )
    current_price = serializers.SerializerMethodField()
    sku_count = serializers.IntegerField(read_only=True)

    class Meta:
        model = Garment
        fields = (
            "id",
            "name",
            "school_level",
            "school_level_display",
            "colour",
            "is_active",
            "current_price",
            "sku_count",
        )

    def get_current_price(self, obj) -> str | None:
        """Today's price, or null where none is set.

        Null rather than 0. A garment with no price is a master-data gap, and
        showing it as free would be a lie the frontend might act on.

        Reads the annotation the viewset attaches, so a list of 45 garments
        costs one query rather than 46. Falls back to a direct lookup for an
        object that was not annotated — a freshly created one, for instance.
        """
        return _annotated_price(obj) if _is_annotated(obj) else _looked_up_price(obj)


# ---------------------------------------------------------------------------
# Pricing
# ---------------------------------------------------------------------------


class GarmentPriceSerializer(serializers.ModelSerializer):
    garment_name = serializers.CharField(source="garment.__str__", read_only=True)

    class Meta:
        model = GarmentPrice
        fields = (
            "id",
            "garment",
            "garment_name",
            "unit_price",
            "active_date",
            "expiration_date",
        )

    def validate(self, attrs):
        """Run the model's own rules, so the database never has to refuse.

        The price table is guarded by a check constraint and an exclusion
        constraint. Left to the database, a violation arrives as an
        IntegrityError — a 500. Running `full_clean()` here turns it into a
        400 naming the field.

        The instance is **copied** rather than rebuilt. An earlier version
        constructed a fresh `GarmentPrice` and assigned its `pk`, which left
        `_state.adding` True: Django then treated a legitimate correction as a
        new row with a duplicate primary key and refused it. A copy carries
        that state, so `full_clean()` correctly excludes the row from its own
        overlap check.
        """
        instance = copy.deepcopy(self.instance) if self.instance else GarmentPrice()
        for field, value in attrs.items():
            setattr(instance, field, value)

        instance.full_clean()
        return attrs


class RepriceSerializer(serializers.Serializer):
    """Input for the reprice action — the sanctioned way to change a price."""

    unit_price = serializers.DecimalField(
        max_digits=12, decimal_places=2, min_value=Decimal("0.01")
    )
    active_from = serializers.DateField(
        help_text="First day the new price applies. The current price is closed on this date."
    )


class PriceListRowSerializer(serializers.Serializer):
    """One line of a PS or HS price list."""

    garment_id = serializers.IntegerField(source="garment.id")
    garment = serializers.CharField(source="garment.__str__")
    colour = serializers.CharField(source="garment.colour")
    unit_price = serializers.DecimalField(max_digits=12, decimal_places=2)


# ---------------------------------------------------------------------------
# SKUs
# ---------------------------------------------------------------------------


class SkuSerializer(serializers.ModelSerializer):
    garment_name = serializers.CharField(source="garment.__str__", read_only=True)
    size_name = serializers.CharField(source="size.name", read_only=True)
    unit_price = serializers.SerializerMethodField()

    class Meta:
        model = Sku
        fields = (
            "id",
            "number",
            "garment",
            "garment_name",
            "size",
            "size_name",
            "description",
            "is_active",
            "unit_price",
        )
        # The control number is assigned by the system and never changes —
        # it is printed on pick lists and packing lists.
        read_only_fields = ("id", "number")

    def get_unit_price(self, obj) -> str | None:
        """Read through to the garment's price. SKUs carry no price of their own.

        Annotated in bulk by the viewset — see GarmentSerializer.get_current_price.
        """
        return _annotated_price(obj) if _is_annotated(obj) else _looked_up_price(obj.garment)


class MinimumStockLevelSerializer(serializers.ModelSerializer):
    sku_number = serializers.CharField(source="sku.number", read_only=True)
    sku_description = serializers.CharField(source="sku.description", read_only=True)
    warehouse_name = serializers.CharField(source="warehouse.name", read_only=True)

    class Meta:
        model = MinimumStockLevel
        fields = (
            "id",
            "sku",
            "sku_number",
            "sku_description",
            "warehouse",
            "warehouse_name",
            "minimum_quantity",
        )


# ---------------------------------------------------------------------------
# Price rendering
# ---------------------------------------------------------------------------
# Both Garment and Sku expose "the price today". Shared here so the two cannot
# drift in how they represent an unpriced item.


def _is_annotated(obj) -> bool:
    return hasattr(obj, CURRENT_PRICE_ANNOTATION)


def _annotated_price(obj) -> str | None:
    amount = getattr(obj, CURRENT_PRICE_ANNOTATION)
    return str(amount) if amount is not None else None


def _looked_up_price(garment) -> str | None:
    try:
        return str(price_for(garment))
    except PriceNotSet:
        return None
