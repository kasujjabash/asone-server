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

    # F42: **one despatch, many orders.** AsOne's checklist asks for a
    # "consolidated weekly despatch" per school, and the school then hands
    # parcels to students by name off the packing list. So the consignee is
    # the school, and which orders are on the van is a fact about the lines.
    #
    # This replaced a single `order` FK. A shipment carrying one order is
    # still the common case — `ship_order()` makes exactly that — but it is
    # now a special case of the general shape rather than the only shape.
    school = models.ForeignKey(
        "catalog.School",
        on_delete=models.PROTECT,
        related_name="shipments",
        help_text="Who receives it. Every order on a shipment belongs to this school.",
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

    # How it travelled — "Internal Route Truck #4". Free text and optional,
    # for the same reason the packing list number is: it is written on a
    # sheet of paper at the gate, and a clerk who was not told must still be
    # able to record the despatch.
    carrier_method = models.CharField(
        max_length=120,
        blank=True,
        help_text="How it travelled — a truck, a route, a courier. As given at the gate.",
    )
    shipped_by = models.ForeignKey(
        "accounts.User", on_delete=models.PROTECT, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    # Set when the school confirms the parcel arrived — F41's other half.
    #
    # On the shipment rather than the order, deliberately. An order can have
    # two shipments: decision D2 lets a backorder go direct from a different
    # warehouse. A single flag on the order would let a school confirm the
    # first parcel and close an order still waiting on the second.
    received_at = models.DateTimeField(null=True, blank=True)
    received_by = models.ForeignKey(
        "accounts.User",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="+",
    )
    receipt_notes = models.TextField(
        blank=True,
        help_text="Anything wrong with the parcel — short, damaged, wrong student.",
    )

    waybill_number = models.CharField(
        max_length=50,
        blank=True,
        help_text="The carrier's reference, if there is one.",
    )
    notes = models.TextField(blank=True)

    class Meta:
        ordering = ["-shipped_on", "-number"]
        indexes = [
            # "What is coming to this school" and "what left this warehouse
            # this week" — the two questions the shipping screens ask.
            models.Index(fields=["school", "-shipped_on"]),
            models.Index(fields=["from_warehouse", "shipped_on"]),
            # Parcels nobody has confirmed: the school dashboard's actionable
            # list, and the gap F42's completion rule turns on.
            models.Index(fields=["received_at"]),
        ]

    def __str__(self):
        return f"{self.number} from {self.from_warehouse.name}"

    @property
    def is_received(self) -> bool:
        return self.received_at is not None

    @property
    def orders(self):
        """The distinct orders on this van, in number order.

        Derived from the lines rather than stored: a shipment *is* its lines,
        and a second list of orders could disagree with them.
        """
        from orders.models.school_orders import SchoolOrder

        return (
            SchoolOrder.objects.filter(shipment_lines__shipment=self)
            .distinct()
            .order_by("number")
        )

    @property
    def order_count(self) -> int:
        """How many orders are on it — the design's "Orders" column.

        Counted over prefetched lines so a list does not fan out per row.
        """
        return len({line.order_id for line in self.lines.all()})

    @property
    def status(self) -> str:
        """Where this parcel is — derived, never stored.

        Two stored facts answer it, which is why there is no status column:
        `shipped_on` says it left, `received_at` says it arrived. A third
        state would be a third thing to keep in step with them.

        Deliberately **not** the design's "Preparing"/"Ready" pair. Those
        describe how far a warehouse has got with picking, which is a fact
        about the *order*, not about a parcel that does not exist until
        `ship_order()` creates it. A shipment row is only ever created at
        despatch, so it is never "preparing".
        """
        return "DELIVERED" if self.received_at else "SHIPPED"

    @property
    def total_quantity(self) -> int:
        """Units on the van. Summed over prefetched lines, like an order."""
        return sum(line.quantity for line in self.lines.all())

    def save(self, *args, **kwargs):
        if not self.number:
            from orders.services.shipping import next_shipment_number

            self.number = next_shipment_number()
        super().save(*args, **kwargs)


class ShipmentLine(models.Model):
    """One SKU on a shipment, for one order.

    Lines exist because a shipment is not always the whole order: a short
    pick leaves a backorder, and what is on the van is only what was there.

    **`order` is what makes F42 work.** A consolidated despatch carries
    several students' uniforms, and the packing list has to say which parcel
    is whose — AsOne's note is that the school "distributes to students by
    name on the packing list". Without the order on the line, a van holding
    four shirts could not say which two are Grace's.
    """

    shipment = models.ForeignKey(
        Shipment, on_delete=models.CASCADE, related_name="lines"
    )
    order = models.ForeignKey(
        "orders.SchoolOrder",
        on_delete=models.PROTECT,
        related_name="shipment_lines",
        help_text="Which order this line fills, and so which student it is for.",
    )
    sku = models.ForeignKey("catalog.Sku", on_delete=models.PROTECT, related_name="+")
    quantity = models.PositiveIntegerField(validators=[MinValueValidator(1)])

    class Meta:
        ordering = ["order__number", "sku__description"]
        constraints = [
            # Per *order*, not per shipment: two students on the same van
            # both getting a size 8 shirt is two lines of the same SKU, and
            # collapsing them would lose whose is whose.
            models.UniqueConstraint(
                fields=["shipment", "order", "sku"],
                name="unique_sku_per_order_per_shipment",
            ),
        ]

    def __str__(self):
        return f"{self.quantity} x {self.sku.number} for {self.order.number}"
