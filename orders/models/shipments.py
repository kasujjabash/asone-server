"""A shipment — F41.

What physically left a warehouse, when, and for which order.

## Why a shipment is not just a status on the order

Decision D2, Jim on 24 August: a school orders on its primary warehouse, but
a **backorder may be filled by a different warehouse shipping direct to the
school**. So one order can leave from two places, on two days.

That is why `from_warehouse` is a field here and is never derived from
`order.school.primary_warehouse`. For a transferred backorder they differ,
and deriving it would quietly ship from the wrong place.

## The part AsOne has not answered

Their own outbound chart reads "Inventory moves from a 'Pick' status to
'Shipped' ???" — the question marks are theirs. What we have settled, because
the ledger forced it, is that stock is **committed at pick** and **leaves at
ship**. What is still open is which real-world event sets Shipped: the moment
a van is loaded, or the moment the school confirms it arrived.

That distinction does not change this model. `shipped_on` is when it left.
If AsOne says arrival is what counts, that is an added confirmation field —
`received_at`, set by the school — not a change to what is written here.
See `orders/services/shipping.py::ship_order`.
"""

from django.core.validators import MinValueValidator
from django.db import models


class Shipment(models.Model):
    """One despatch from one warehouse for one order."""

    number = models.CharField(
        max_length=16,
        unique=True,
        editable=False,
        help_text="System assigned. Never reused.",
    )

    order = models.ForeignKey(
        "orders.SchoolOrder", on_delete=models.PROTECT, related_name="shipments"
    )

    # Never derived from the school's primary warehouse — see D2 in the
    # module docstring. A backorder filled elsewhere ships from elsewhere.
    from_warehouse = models.ForeignKey(
        "catalog.Warehouse",
        on_delete=models.PROTECT,
        related_name="shipments",
        help_text="Where this actually left from, which is not always the school's own warehouse.",
    )

    shipped_on = models.DateField(help_text="The day it left the warehouse.")
    shipped_by = models.ForeignKey(
        "accounts.User", on_delete=models.PROTECT, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    waybill_number = models.CharField(
        max_length=50,
        blank=True,
        help_text="The carrier's reference, if there is one.",
    )
    notes = models.TextField(blank=True)

    class Meta:
        ordering = ["-shipped_on", "-number"]
        indexes = [
            models.Index(fields=["order"]),
            models.Index(fields=["from_warehouse", "shipped_on"]),
        ]

    def __str__(self):
        return f"{self.number} from {self.from_warehouse.name}"

    @property
    def total_quantity(self) -> int:
        return sum(line.quantity for line in self.lines.all())

    def save(self, *args, **kwargs):
        if not self.number:
            from orders.services.shipping import next_shipment_number

            self.number = next_shipment_number()
        super().save(*args, **kwargs)


class ShipmentLine(models.Model):
    """One SKU on a shipment.

    Lines exist because a shipment is not always the whole order: a short
    pick leaves a backorder, and what is on the van is only what was there.
    """

    shipment = models.ForeignKey(
        Shipment, on_delete=models.CASCADE, related_name="lines"
    )
    sku = models.ForeignKey("catalog.Sku", on_delete=models.PROTECT, related_name="+")
    quantity = models.PositiveIntegerField(validators=[MinValueValidator(1)])

    class Meta:
        ordering = ["sku__description"]
        constraints = [
            models.UniqueConstraint(
                fields=["shipment", "sku"], name="unique_sku_per_shipment"
            ),
        ]

    def __str__(self):
        return f"{self.quantity} x {self.sku.number}"
