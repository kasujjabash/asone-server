"""Shared shape for the order documents in this app.

A group order and a production order are the same kind of thing: a numbered
document, dated, with a status and lines of SKU-and-quantity. What differs is
who it is addressed to. That commonality lives here so the two cannot drift
apart in ways nobody intended.
"""

from django.core.validators import MinValueValidator
from django.db import models


class OrderStatus(models.TextChoices):
    """Where a document is in its life.

    Deliberately small. AsOne's pack describes no approval step for these,
    so inventing one would be inventing a requirement.
    """

    OPEN = "OPEN", "Open"
    CLOSED = "CLOSED", "Closed"
    CANCELLED = "CANCELLED", "Cancelled"


class OrderDocument(models.Model):
    """Abstract base for a numbered, dated order document."""

    number = models.CharField(
        max_length=16,
        unique=True,
        editable=False,
        help_text="System assigned. Unique forever, never reused.",
    )
    order_date = models.DateField(help_text="The date the order was placed.")
    due_in_warehouse_date = models.DateField(
        null=True, blank=True, help_text="When the goods are expected."
    )
    status = models.CharField(
        max_length=12, choices=OrderStatus.choices, default=OrderStatus.OPEN
    )
    notes = models.TextField(blank=True)

    # Every transaction records the user — AsOne asked for this explicitly
    # (p.9), and PROTECT means the person who raised an order cannot later be
    # erased from it.
    created_by = models.ForeignKey(
        "accounts.User", on_delete=models.PROTECT, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        abstract = True
        ordering = ["-order_date", "-number"]

    def __str__(self):
        return self.number

    @property
    def total_quantity(self) -> int:
        return sum(line.quantity for line in self.lines.all())

    @property
    def total_value(self):
        return sum(line.line_total for line in self.lines.all())


class OrderLine(models.Model):
    """Abstract base for a line of SKU, quantity and unit price.

    **`unit_price` is a snapshot, not a lookup.** An order is a commitment at
    a price agreed on the day it was placed. Repricing a garment in March must
    not silently restate what a January order was worth, so the figure is
    copied onto the line at creation and never recomputed. AsOne's own header
    layout (p.4) lists Unit Price as a line field for the same reason.
    """

    sku = models.ForeignKey("catalog.Sku", on_delete=models.PROTECT, related_name="+")
    quantity = models.PositiveIntegerField(validators=[MinValueValidator(1)])
    unit_price = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        help_text="The price when the order was placed. Not recalculated later.",
    )

    class Meta:
        abstract = True
        ordering = ["sku__description"]

    def __str__(self):
        return f"{self.sku.number} x {self.quantity}"

    @property
    def line_total(self):
        return self.unit_price * self.quantity
