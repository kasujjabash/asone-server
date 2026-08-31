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
            "lines",
        )
        read_only_fields = (
            "id",
            "number",
            "school",
            "status",
            "created_by",
            "created_at",
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
