"""SKUs and their per-warehouse minimum stock levels.

A SKU is one garment in one size — "White Shirt size 10". It is what gets
counted, ordered, picked and shipped. A garment on its own is never in stock.

About 200 of them: ~45 garments in four or five sizes each.
"""

from django.core.validators import MinValueValidator
from django.db import models


class Sku(models.Model):
    """One garment in one size.

    `number` is AsOne's control number: system assigned, unique, and **never
    reused**, even after a SKU is retired. It is drawn from a Postgres
    sequence rather than from a count of existing rows — a count would hand
    the same number to two people creating SKUs at the same moment, and would
    recycle numbers after a deletion.
    """

    number = models.CharField(
        max_length=12,
        unique=True,
        editable=False,
        help_text="System assigned. Unique forever, never reused.",
    )
    garment = models.ForeignKey(
        "catalog.Garment", on_delete=models.PROTECT, related_name="skus"
    )
    size = models.ForeignKey(
        "catalog.Size", on_delete=models.PROTECT, related_name="skus"
    )

    # Stored rather than computed on read: pick lists print "in Description
    # sequence" (p.2), and ordering by a stored column is something the
    # database can do with an index.
    description = models.CharField(
        max_length=200,
        blank=True,
        help_text="Filled in from the garment and size if left blank.",
    )
    is_active = models.BooleanField(
        default=True, help_text="Inactive SKUs stay in reports but cannot be ordered."
    )

    class Meta:
        ordering = ["description"]
        constraints = [
            # One SKU per garment-and-size. Two rows for "White Shirt size 10"
            # would split its stock across two control numbers, and neither
            # would show the true quantity on hand.
            models.UniqueConstraint(
                fields=["garment", "size"], name="unique_sku_per_garment_size"
            )
        ]

    def __str__(self):
        return f"{self.number} — {self.description}"

    @property
    def unit_price(self):
        """This SKU's price today, taken from its garment.

        There is no price on a SKU. Price does not vary by size, so it lives
        on the garment and every size reads through to it — which is what
        makes the rule impossible to violate rather than merely documented.

        Raises `catalog.services.PriceNotSet` if the garment has no price
        today.
        """
        from catalog.services import price_for

        return price_for(self.garment)

    def build_description(self) -> str:
        """The human label: "White Shirt Blue size 10 (PS)"."""
        parts = [self.garment.name]
        if self.garment.colour:
            parts.append(self.garment.colour)
        parts.append(f"size {self.size.name}")

        label = " ".join(parts)
        if self.garment.school_level != self.garment.SchoolLevel.BOTH:
            label = f"{label} ({self.garment.school_level})"
        return label

    def save(self, *args, **kwargs):
        # Assigned here rather than in a signal, so it is visible where it
        # happens. Only ever set when missing: an existing number must never
        # change, because it is printed on pick lists and packing lists.
        if not self.number:
            from catalog.services import next_sku_number

            self.number = next_sku_number()

        if not self.description:
            self.description = self.build_description()

        super().save(*args, **kwargs)


class MinimumStockLevel(models.Model):
    """The level that triggers a replenishment order on the Tailoring Centers.

    Held per SKU **per warehouse** — p.3 says "Minimum Inventory level for
    each warehouse", and the two warehouses serve different numbers of
    schools, so the same shirt needs a different floor at each.

    A maximum level appeared in the 10 August pack and was dropped in the
    14 August revision, so there is deliberately no maximum here.
    """

    sku = models.ForeignKey(Sku, on_delete=models.PROTECT, related_name="minimum_levels")
    warehouse = models.ForeignKey(
        "catalog.Warehouse", on_delete=models.PROTECT, related_name="minimum_levels"
    )
    minimum_quantity = models.PositiveIntegerField(
        validators=[MinValueValidator(0)],
        help_text="Stock at or below this level raises a reorder alert.",
    )

    class Meta:
        ordering = ["warehouse", "sku"]
        constraints = [
            models.UniqueConstraint(
                fields=["sku", "warehouse"], name="unique_minimum_per_sku_warehouse"
            )
        ]

    def __str__(self):
        return f"{self.sku.number} at {self.warehouse.name}: {self.minimum_quantity}"
