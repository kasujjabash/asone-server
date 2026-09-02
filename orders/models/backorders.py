"""A backorder — F43, F44, F45, F46.

What a school ordered and its own warehouse could not supply.

## Why this is a row and not a calculation

A shortfall could be derived: order lines minus what was picked. It is stored
instead because a backorder has a *life* the arithmetic cannot hold — it gets
assigned to another warehouse, that warehouse ships it direct, and somebody
has to be able to ask "what is outstanding, and who is filling it".

Decision D2, Jim on 24 August, is the rule this exists to serve:

> "The warehouses should have the capability to transfer 'Backorders' to
> another warehouse with Inventory. The fulfilling warehouse will then ship
> directly to the appropriate school."

So a backorder carries `filled_by_warehouse` — which is *not* the school's
own. That is the whole point of it.
"""

from django.core.validators import MinValueValidator
from django.db import models


class BackorderStatus(models.TextChoices):
    """Where a backorder is in its life.

    OPEN means nobody has taken it on yet. ASSIGNED means a warehouse with
    stock has accepted it (F45) and has not yet shipped. FILLED means it
    left, and the shipment says from where.
    """

    OPEN = "OPEN", "Outstanding — no warehouse assigned"
    ASSIGNED = "ASSIGNED", "Assigned to a warehouse with stock"
    FILLED = "FILLED", "Shipped to the school"
    CANCELLED = "CANCELLED", "Cancelled"


#: Named for the generated API client, so it does not become Status123Enum.
BACKORDER_STATUS_CHOICES = BackorderStatus.choices


class Backorder(models.Model):
    """One SKU a school is still owed on one order."""

    order = models.ForeignKey(
        "orders.SchoolOrder", on_delete=models.PROTECT, related_name="backorders"
    )
    sku = models.ForeignKey("catalog.Sku", on_delete=models.PROTECT, related_name="+")
    quantity = models.PositiveIntegerField(validators=[MinValueValidator(1)])

    status = models.CharField(
        max_length=10, choices=BackorderStatus.choices, default=BackorderStatus.OPEN
    )

    # Null until a warehouse takes it on. Never derived from the school's
    # primary warehouse — that is the warehouse that could not supply it.
    filled_by_warehouse = models.ForeignKey(
        "catalog.Warehouse",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="backorders_accepted",
        help_text="The warehouse that took this on. Not the school's own — that is the one that ran short.",
    )
    assigned_at = models.DateTimeField(null=True, blank=True)
    assigned_by = models.ForeignKey(
        "accounts.User", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )

    created_at = models.DateTimeField(auto_now_add=True)
    created_by = models.ForeignKey(
        "accounts.User", on_delete=models.PROTECT, related_name="+"
    )
    notes = models.TextField(blank=True)

    class Meta:
        ordering = ["created_at", "sku__description"]
        indexes = [
            models.Index(fields=["status"]),
            models.Index(fields=["filled_by_warehouse", "status"]),
        ]
        constraints = [
            # One backorder per SKU per order. A second short pick on the
            # same shirt is the same debt, not a new one.
            models.UniqueConstraint(
                fields=["order", "sku"], name="unique_backorder_per_order_sku"
            ),
        ]

    def __str__(self):
        return f"{self.quantity} x {self.sku.number} owed on {self.order.number}"

    @property
    def origin_warehouse(self):
        """The warehouse that could not supply it — the school's own."""
        return self.order.warehouse

    @property
    def can_be_assigned(self) -> bool:
        return self.status == BackorderStatus.OPEN
