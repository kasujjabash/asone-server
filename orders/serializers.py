"""Serializers for school orders.

Shape only. The interesting asymmetry is between reading and writing: a
school *writes* kits and items, and *reads back* the exploded SKU lines the
warehouse will pick. Those are deliberately different shapes, because they
answer different questions.
"""

from rest_framework import serializers

from catalog.models import Kit, Sku, Warehouse

from .models import Backorder, SchoolOrder, SchoolOrderLine, Shipment, ShipmentLine


class SchoolOrderLineSerializer(serializers.ModelSerializer):
    """One line as the warehouse and the invoice see it — always a SKU."""

    sku_number = serializers.CharField(source="sku.number", read_only=True)
    sku_description = serializers.CharField(source="sku.description", read_only=True)
    from_kit_number = serializers.CharField(
        source="from_kit.kit_number", read_only=True, default=None
    )
    line_total = serializers.DecimalField(
        max_digits=14, decimal_places=2, read_only=True
    )

    class Meta:
        model = SchoolOrderLine
        fields = (
            "id",
            "sku",
            "sku_number",
            "sku_description",
            "quantity",
            "unit_price",
            "line_total",
            "from_kit",
            "from_kit_number",
        )
        read_only_fields = fields


class KitLineInputSerializer(serializers.Serializer):
    """A kit the school chose. Exploded into SKUs on save."""

    kit = serializers.PrimaryKeyRelatedField(queryset=Kit.objects.all())
    quantity = serializers.IntegerField(min_value=1, default=1)


class SkuLineInputSerializer(serializers.Serializer):
    """An individual item the school chose."""

    sku = serializers.PrimaryKeyRelatedField(queryset=Sku.objects.all())
    quantity = serializers.IntegerField(min_value=1)


class InvoiceKitGroupSerializer(serializers.Serializer):
    """A kit on the invoice, with the garments it contains beneath it.

    AsOne's definition of an invoice names the kit number "if used", so the
    school sees the thing it actually chose rather than a flat list of
    garments it did not ask for by name.
    """

    kit_number = serializers.CharField(source="kit.kit_number")
    kit_name = serializers.CharField(source="kit.name")
    subtotal = serializers.DecimalField(max_digits=14, decimal_places=2)
    lines = SchoolOrderLineSerializer(many=True)


class InvoiceSerializer(serializers.Serializer):
    """The order as a document — F34.

    Same number as the order: AsOne treats the two as one thing, since the
    school uses the invoice number and the student's name to hand the right
    parcel to the right child.
    """

    number = serializers.CharField(help_text="The invoice number, also the order number.")
    student_name = serializers.CharField()
    school_name = serializers.CharField(source="school.name")
    order_date = serializers.DateField()
    status = serializers.CharField()
    status_display = serializers.CharField(source="get_status_display")
    total = serializers.DecimalField(max_digits=14, decimal_places=2)

    kits = InvoiceKitGroupSerializer(many=True)
    items = SchoolOrderLineSerializer(many=True, help_text="Ordered individually.")

    cancelled_at = serializers.DateTimeField(allow_null=True)
    cancellation_reason = serializers.CharField(allow_blank=True)


class CancelOrderSerializer(serializers.Serializer):
    """Withdrawing an unpaid invoice — F36."""

    reason = serializers.CharField(
        max_length=200,
        required=False,
        allow_blank=True,
        default="",
        help_text="Why the school cancelled. Optional, but useful when a parent asks.",
    )


class ReleaseOrderSerializer(serializers.Serializer):
    """Confirming payment on an order — F35.

    `payment_reference` is free text because we do not yet know what the
    real one looks like: what confirms payment is open question Q2.
    """

    payment_reference = serializers.CharField(
        max_length=100,
        required=False,
        allow_blank=True,
        default="",
        help_text="Receipt number, mobile money reference — whatever identifies the payment.",
    )


class ShipmentLineSerializer(serializers.ModelSerializer):
    sku_number = serializers.CharField(source="sku.number", read_only=True)
    sku_description = serializers.CharField(source="sku.description", read_only=True)

    class Meta:
        model = ShipmentLine
        fields = ("id", "sku", "sku_number", "sku_description", "quantity")


class ShipmentSerializer(serializers.ModelSerializer):
    """What left a warehouse — F41."""

    from_warehouse_name = serializers.CharField(
        source="from_warehouse.name", read_only=True
    )
    order_number = serializers.CharField(source="order.number", read_only=True)
    shipped_by_name = serializers.CharField(
        source="shipped_by.get_full_name", read_only=True
    )
    lines = ShipmentLineSerializer(many=True, read_only=True)

    class Meta:
        model = Shipment
        fields = (
            "id",
            "number",
            "order",
            "order_number",
            "from_warehouse",
            "from_warehouse_name",
            "shipped_on",
            "shipped_by",
            "shipped_by_name",
            "waybill_number",
            "notes",
            "lines",
        )
        read_only_fields = fields


class ShipOrderSerializer(serializers.Serializer):
    """Sending a picked order out — F41.

    `from_warehouse` is optional and defaults to the order's own. It exists
    because decision D2 lets a backorder ship direct from whichever
    warehouse actually filled it.
    """

    from_warehouse = serializers.PrimaryKeyRelatedField(
        queryset=Warehouse.objects.all(),
        required=False,
        allow_null=True,
        help_text="Defaults to the school's own warehouse. Set for a backorder filled elsewhere.",
    )
    shipped_on = serializers.DateField(
        required=False, help_text="Defaults to today."
    )
    waybill_number = serializers.CharField(
        max_length=50, required=False, allow_blank=True, default=""
    )
    notes = serializers.CharField(required=False, allow_blank=True, default="")


class OrderOnHoldSerializer(serializers.ModelSerializer):
    """One row of the still-on-hold report — F53."""

    school_name = serializers.CharField(source="school.name", read_only=True)
    total = serializers.DecimalField(max_digits=14, decimal_places=2, read_only=True)
    line_count = serializers.SerializerMethodField()

    class Meta:
        model = SchoolOrder
        fields = (
            "id",
            "number",
            "school",
            "school_name",
            "student_name",
            "order_date",
            "total",
            "line_count",
        )
        read_only_fields = fields

    def get_line_count(self, obj) -> int:
        return len(obj.lines.all())


class SchoolOrderSerializer(serializers.ModelSerializer):
    """An order, reading. Doubles as the invoice — same number, same lines."""

    lines = SchoolOrderLineSerializer(many=True, read_only=True)
    school_name = serializers.CharField(source="school.name", read_only=True)
    warehouse_name = serializers.CharField(source="warehouse.name", read_only=True)
    status_display = serializers.CharField(source="get_status_display", read_only=True)
    created_by_name = serializers.CharField(
        source="created_by.get_full_name", read_only=True
    )
    total = serializers.DecimalField(max_digits=14, decimal_places=2, read_only=True)

    class Meta:
        model = SchoolOrder
        fields = (
            "id",
            "number",
            "school",
            "school_name",
            "warehouse_name",
            "student_name",
            "order_date",
            "status",
            "status_display",
            "notes",
            "total",
            "created_by",
            "created_by_name",
            "created_at",
            "cancelled_at",
            "cancellation_reason",
            "released_at",
            "payment_reference",
            "lines",
        )
        read_only_fields = (
            "id",
            "number",
            "school",
            "status",
            "created_by",
            "created_at",
            "cancelled_at",
            "cancellation_reason",
            "released_at",
            "payment_reference",
        )


class SchoolOrderWriteSerializer(serializers.Serializer):
    """Placing an order — F30, F31.

    `school` is not a field. A school clerk orders for their own school and
    no other; taking it from the request body would invite a school to place
    an order against someone else's.

    `status` is not a field either. Every order starts on Hold (F32) and
    only payment confirmation moves it — see open question Q2.
    """

    student_name = serializers.CharField(
        max_length=150,
        help_text="The student this uniform is for. Free text — students have no accounts.",
    )
    order_date = serializers.DateField()
    kits = KitLineInputSerializer(many=True, required=False, default=list)
    skus = SkuLineInputSerializer(many=True, required=False, default=list)
    notes = serializers.CharField(required=False, allow_blank=True, default="")

    def validate_student_name(self, value):
        if not value.strip():
            raise serializers.ValidationError(
                "A student's name is required — it is how the school hands the "
                "uniform to the right child."
            )
        return value.strip()

    def validate(self, attrs):
        if not attrs.get("kits") and not attrs.get("skus"):
            raise serializers.ValidationError(
                {"skus": "An order needs at least one kit or item."}
            )
        return attrs


class OrderDemandRowSerializer(serializers.Serializer):
    """One SKU the warehouse has to pick, however many places it came from."""

    sku_number = serializers.CharField()
    sku_description = serializers.CharField()
    quantity = serializers.IntegerField()


class OrderAvailabilityRowSerializer(serializers.Serializer):
    """One SKU on the order, and whether there is enough of it — F37."""

    sku_number = serializers.CharField(source="sku.number")
    sku_description = serializers.CharField(source="sku.description")
    needed = serializers.IntegerField()
    available = serializers.IntegerField()
    shortfall = serializers.IntegerField(
        help_text="How many short. Zero means this line can be filled."
    )


class BackorderSerializer(serializers.ModelSerializer):
    """What a school is still owed — F44."""

    order_number = serializers.CharField(source="order.number", read_only=True)
    school_name = serializers.CharField(source="order.school.name", read_only=True)
    student_name = serializers.CharField(source="order.student_name", read_only=True)
    sku_number = serializers.CharField(source="sku.number", read_only=True)
    sku_description = serializers.CharField(source="sku.description", read_only=True)
    status_display = serializers.CharField(source="get_status_display", read_only=True)
    origin_warehouse_name = serializers.CharField(
        source="order.school.primary_warehouse.name",
        read_only=True,
        help_text="The warehouse that ran short.",
    )
    filled_by_warehouse_name = serializers.CharField(
        source="filled_by_warehouse.name", read_only=True, default=None
    )

    class Meta:
        model = Backorder
        fields = (
            "id", "order", "order_number", "school_name", "student_name",
            "sku", "sku_number", "sku_description", "quantity",
            "status", "status_display",
            "origin_warehouse_name",
            "filled_by_warehouse", "filled_by_warehouse_name",
            "assigned_at", "created_at", "notes",
        )
        read_only_fields = fields


class AssignBackorderSerializer(serializers.Serializer):
    """Handing a backorder to a warehouse that has the stock — F45."""

    warehouse = serializers.PrimaryKeyRelatedField(
        queryset=Warehouse.objects.all(),
        help_text="A warehouse holding enough to fill it — never the one that ran short.",
    )


class FillBackorderSerializer(serializers.Serializer):
    """The assigned warehouse shipping direct to the school — F46."""

    shipped_on = serializers.DateField(required=False, help_text="Defaults to today.")
    waybill_number = serializers.CharField(
        max_length=50, required=False, allow_blank=True, default=""
    )
    notes = serializers.CharField(required=False, allow_blank=True, default="")


class PackingListLineSerializer(serializers.Serializer):
    sku_number = serializers.CharField()
    description = serializers.CharField()
    quantity = serializers.IntegerField()


class PackingListSerializer(serializers.Serializer):
    """The document that travels with the goods — F40."""

    shipment_number = serializers.CharField()
    shipped_on = serializers.DateField()
    waybill_number = serializers.CharField()
    from_warehouse = serializers.CharField()
    invoice_number = serializers.CharField(
        help_text="The order number. Used with the student's name to hand over the parcel."
    )
    student_name = serializers.CharField()
    school = serializers.CharField()
    school_address = serializers.CharField()
    is_direct_from_another_warehouse = serializers.BooleanField(
        help_text="True for a backorder filled elsewhere and shipped direct (D2)."
    )
    lines = PackingListLineSerializer(many=True)
    total_units = serializers.IntegerField()


class PartProcessedOrderSerializer(serializers.ModelSerializer):
    """An order picked but not yet despatched — F52, F54."""

    school_name = serializers.CharField(source="school.name", read_only=True)
    warehouse_name = serializers.CharField(
        source="school.primary_warehouse.name", read_only=True
    )
    total = serializers.DecimalField(max_digits=14, decimal_places=2, read_only=True)

    class Meta:
        model = SchoolOrder
        fields = (
            "id", "number", "school", "school_name", "warehouse_name",
            "student_name", "order_date", "status", "total",
        )
        read_only_fields = fields


class CostedShipmentSerializer(serializers.Serializer):
    """What went to a school and what it was worth — F57."""

    school_id = serializers.IntegerField(source="shipment__order__school_id")
    school_name = serializers.CharField(source="shipment__order__school__name")
    shipments = serializers.IntegerField()
    units = serializers.IntegerField()
    value = serializers.DecimalField(
        max_digits=18, decimal_places=2,
        help_text="Valued at what the school was charged, snapshotted when the order was placed.",
    )
