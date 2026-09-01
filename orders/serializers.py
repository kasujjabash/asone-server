"""Serializers for school orders.

Shape only. The interesting asymmetry is between reading and writing: a
school *writes* kits and items, and *reads back* the exploded SKU lines the
warehouse will pick. Those are deliberately different shapes, because they
answer different questions.
"""

from rest_framework import serializers

from catalog.models import Kit, Sku

from .models import SchoolOrder, SchoolOrderLine


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
