"""Record on each receipt line what the goods were worth.

Hand-written rather than autodetected. `makemigrations` can only offer a
single one-off default for a new non-nullable column, and one default would
be wrong for every line: the value differs per SKU. It has to be copied from
the production order line the goods were received against.

Three steps, in this order:

    1. add the column nullable, so existing rows survive
    2. backfill each row from its order line
    3. make it non-nullable, now that every row has a value

Doing it in one step would either lose the real values or fail on the
existing rows.
"""

from django.db import migrations, models


def backfill_from_order_lines(apps, schema_editor):
    """Copy each receipt line's value from the order line it came from.

    Matched on SKU within the same production order — a receipt line can only
    exist for a SKU that is on the order, which `create_receipt` enforces.
    """
    ReceiptLine = apps.get_model("procurement", "ReceiptLine")
    ProductionOrderLine = apps.get_model("procurement", "ProductionOrderLine")

    lines = ReceiptLine.objects.select_related("receipt").all()
    for line in lines:
        order_line = ProductionOrderLine.objects.filter(
            order_id=line.receipt.production_order_id, sku_id=line.sku_id
        ).first()

        if order_line is None:
            # Should be impossible — create_receipt refuses a SKU that is not
            # on the order. Zero rather than a guess, so it is visibly wrong
            # in the costed report rather than quietly plausible.
            line.unit_value = 0
        else:
            line.unit_value = order_line.unit_price

        line.save(update_fields=["unit_value"])


def clear(apps, schema_editor):
    """Reverse: nothing to undo, the column goes with the AddField."""


class Migration(migrations.Migration):
    dependencies = [("procurement", "0002_receipts")]

    operations = [
        migrations.AddField(
            model_name="receiptline",
            name="unit_value",
            field=models.DecimalField(
                max_digits=12,
                decimal_places=2,
                null=True,
                help_text="What AsOne agreed to pay the TC for one of these. Never recalculated.",
            ),
        ),
        migrations.RunPython(backfill_from_order_lines, clear),
        migrations.AlterField(
            model_name="receiptline",
            name="unit_value",
            field=models.DecimalField(
                max_digits=12,
                decimal_places=2,
                help_text="What AsOne agreed to pay the TC for one of these. Never recalculated.",
            ),
        ),
    ]
