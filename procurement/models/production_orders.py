"""Production Orders — F17.

AsOne's definition (p.2): "Warehouse orders on the TCs. Should initially sum
up to the Group Order."

Two facts from p.4 shape this:

    "Warehouses have a primary TC but can order on any TC"

so the Tailoring Center is chosen per order rather than derived from the
warehouse — `Warehouse.primary_tailoring_center` is only a default.

    Header: Production Order #, Date, Due in Warehouse Date, TC, Ship to
    Warehouse

The TC makes the goods; the warehouse receives them. Both are on the order
because they are genuinely different sites.
"""

from django.db import models

from .base import OrderDocument, OrderLine


class ProductionOrder(OrderDocument):
    """One warehouse's order on one Tailoring Center."""

    tailoring_center = models.ForeignKey(
        "catalog.TailoringCenter",
        on_delete=models.PROTECT,
        related_name="production_orders",
        help_text="Who makes the goods. Any TC, not only the warehouse's primary one.",
    )
    warehouse = models.ForeignKey(
        "catalog.Warehouse",
        on_delete=models.PROTECT,
        related_name="production_orders",
        help_text="Who receives the goods.",
    )

    # Optional. The first season's orders break down a group order, but
    # reorders and emergency orders later in the year have no group order
    # behind them — and Q11 asks whether group orders survive the capital
    # phase at all. A required link would have prejudged that.
    group_order = models.ForeignKey(
        "procurement.GroupOrder",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="production_orders",
        help_text="The group order this breaks down, where there is one.",
    )

    class Meta(OrderDocument.Meta):
        abstract = False
        verbose_name = "production order"
        indexes = [
            # Warehouse staff see only their own warehouse's orders, and the
            # open-orders view filters on status — both are hot paths.
            models.Index(fields=["warehouse", "status"]),
        ]

    def save(self, *args, **kwargs):
        if not self.number:
            from procurement.services import next_production_order_number

            self.number = next_production_order_number()
        super().save(*args, **kwargs)


class ProductionOrderLine(OrderLine):
    """One SKU on a production order."""

    order = models.ForeignKey(
        ProductionOrder, on_delete=models.CASCADE, related_name="lines"
    )

    class Meta(OrderLine.Meta):
        abstract = False
        constraints = [
            models.UniqueConstraint(
                fields=["order", "sku"], name="unique_sku_per_production_order"
            )
        ]
