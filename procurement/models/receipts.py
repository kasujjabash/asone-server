"""Receipts — F19, F20, F21.

AsOne's flow (p.5), in order:

    TCs manufacture and ship to the warehouse with a **handwritten** packing
    list. The warehouse checks what arrived against that packing list,
    resolving any differences. The warehouse then keys the receipt into the
    system, and posting it increments inventory.

Two things follow from "handwritten":

**Tailoring Centers are not system users.** Nobody at a TC types anything.
The packing list arrives on paper with the goods, and a warehouse clerk keys
it in. That is why `packing_list_number` is free text — it is a number
somebody wrote by hand on a sheet of paper.

**What arrived and what the paper claims are different facts** (F20). The
count is the truth; the packing list is what the TC believed it sent. A
receipt records both, so a discrepancy is visible rather than silently
resolved in favour of whichever was keyed in.
"""

from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator
from django.db import models


class Receipt(models.Model):
    """Goods arriving at a warehouse against a production order.

    A production order may be received more than once — a TC delivering 500
    shirts in two vans produces two receipts. Nothing here assumes one
    delivery closes an order.
    """

    number = models.CharField(
        max_length=16,
        unique=True,
        editable=False,
        help_text="System assigned. Unique forever, never reused.",
    )

    production_order = models.ForeignKey(
        "procurement.ProductionOrder",
        on_delete=models.PROTECT,
        related_name="receipts",
        help_text="What this delivery is against.",
    )

    # Free text: it is a number handwritten on a sheet of paper that travelled
    # with the goods. Not unique — two Tailoring Centers may number their own
    # books independently, and neither is wrong.
    packing_list_number = models.CharField(
        max_length=60,
        help_text="From the TC's handwritten packing list, exactly as written.",
    )
    date_received = models.DateField()
    notes = models.TextField(blank=True)

    #: Posting is what writes to the ledger. Kept as a flag rather than
    #: inferred, so a receipt can be entered, checked against the paper, and
    #: posted deliberately — the check is the point of the step (p.5).
    posted_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="When this receipt was posted to inventory. Blank means not yet posted.",
    )

    created_by = models.ForeignKey(
        "accounts.User", on_delete=models.PROTECT, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-date_received", "-number"]
        indexes = [models.Index(fields=["production_order", "-date_received"])]

    def __str__(self):
        return f"{self.number} against {self.production_order.number}"

    @property
    def is_posted(self) -> bool:
        return self.posted_at is not None

    @property
    def warehouse(self):
        """Where the goods landed. Taken from the order, never re-entered.

        Re-keying it would let a receipt claim goods arrived somewhere the
        order never sent them.
        """
        return self.production_order.warehouse

    @property
    def tailoring_center(self):
        """AsOne's "Supplier (Shipping TC)". Also from the order."""
        return self.production_order.tailoring_center

    @property
    def has_discrepancy(self) -> bool:
        return any(line.discrepancy for line in self.lines.all())

    def clean(self):
        super().clean()
        if self.production_order_id and self.date_received:
            if self.date_received < self.production_order.order_date:
                raise ValidationError(
                    {
                        "date_received": (
                            "Goods cannot arrive before the order that asked for "
                            f"them was placed ({self.production_order.order_date})."
                        )
                    }
                )

    def save(self, *args, **kwargs):
        if not self.number:
            from procurement.services import next_receipt_number

            self.number = next_receipt_number()
        super().save(*args, **kwargs)


class ReceiptLine(models.Model):
    """One SKU on a receipt: what arrived, and what the paper said.

    `quantity_received` is the count. `quantity_on_packing_list` is what the
    Tailoring Center wrote down. They are stored separately and never
    reconciled into one figure, because the difference is the thing the
    warehouse is meant to resolve (F20) — averaging them away would hide it.
    """

    receipt = models.ForeignKey(Receipt, on_delete=models.CASCADE, related_name="lines")
    sku = models.ForeignKey("catalog.Sku", on_delete=models.PROTECT, related_name="+")

    quantity_received = models.PositiveIntegerField(
        validators=[MinValueValidator(1)],
        help_text="What was actually counted off the van.",
    )
    quantity_on_packing_list = models.PositiveIntegerField(
        null=True,
        blank=True,
        help_text="What the TC's handwritten packing list claimed. Blank if it did not say.",
    )
    discrepancy_note = models.CharField(
        max_length=200,
        blank=True,
        help_text="Why the count differs — damaged in transit, miscount, short shipment.",
    )

    # Copied from the production order line when the receipt is entered, the
    # same way an order line copies the price list. Stored rather than joined
    # back to for two reasons: the ledger is valued from it, so the document
    # and the ledger cannot disagree; and Finance's costed report becomes a
    # plain sum instead of a correlated join through the order.
    unit_value = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        help_text="What AsOne agreed to pay the TC for one of these. Never recalculated.",
    )

    class Meta:
        ordering = ["sku__description"]
        constraints = [
            models.UniqueConstraint(
                fields=["receipt", "sku"], name="unique_sku_per_receipt"
            )
        ]

    def __str__(self):
        return f"{self.sku.number} x {self.quantity_received}"

    @property
    def line_value(self):
        """What actually arrived is worth. A short delivery is worth less."""
        return self.unit_value * self.quantity_received

    @property
    def discrepancy(self) -> int:
        """Counted minus claimed. Positive means more arrived than the paper said.

        Zero when the packing list did not name a quantity — there is nothing
        to disagree with, which is not the same as agreeing.
        """
        if self.quantity_on_packing_list is None:
            return 0
        return self.quantity_received - self.quantity_on_packing_list
