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

from .base import OrderDocument, OrderLine, OrderStatus


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

    @property
    def quantity_received(self) -> int:
        """Units counted in against this order, across **posted** receipts.

        Unposted receipts are excluded for the same reason
        `outstanding_on_order()` excludes them: an unposted receipt is
        paperwork somebody is still checking, not goods the warehouse can
        rely on.

        Summed in Python over prefetched rows rather than annotated, to match
        `total_quantity` above — the viewset prefetches `receipts__lines`, so
        this costs no extra query.
        """
        return sum(
            line.quantity_received
            for receipt in self.receipts.all()
            if receipt.posted_at is not None
            for line in receipt.lines.all()
        )

    @property
    def fulfilment_status(self) -> str:
        """How far along delivery is — derived, not stored.

        **This is not `status`.** `status` is the document's own state, and
        AsOne gave it three values: Open, Closed, Cancelled. The screens want
        to know something else — how much has actually turned up — and that
        is a fact about receipts, not about the order.

        Deliberately silent about the Tailoring Center's own workflow. A
        design mock shows "Draft", "Submitted", "In Production" and "Ready to
        Ship"; none of those are knowable here, because **Tailoring Centers
        are not system users** — nobody at a TC types anything, and the first
        the system hears of a production run is a van at the gate. Inventing
        those states would mean showing a status nothing can ever update.
        """
        if self.status == OrderStatus.CANCELLED:
            return "CANCELLED"
        if self.status == OrderStatus.CLOSED:
            return "CLOSED"

        received = self.quantity_received
        if received <= 0:
            return "AWAITING"
        return "RECEIVED" if received >= self.total_quantity else "PARTIAL"

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
