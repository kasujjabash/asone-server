"""The stock ledger.

**Inventory is an append-only ledger, not a counter.** There is no "quantity
on hand" column anywhere in this project. A stock level is the sum of every
movement affecting that SKU at that warehouse, and it is worked out on read.

That is not a stylistic preference. AsOne asked for audit trails by SKU
(p.9), and a counter cannot answer "why is it 480 rather than 500" — only a
ledger can. Overwriting a quantity destroys the only record of how it got
there.

Two rules follow, and both are enforced on the model rather than trusted:

    A row, once written, is never changed.
    A row, once written, is never deleted.

Corrections are new rows. That is what inventory adjustments (Phase 2) are
for.

The columns AsOne listed for this table (p.5, p.6):

    Transaction #, Document #, Warehouse, Date, SKU #, Quantity,
    Stock Location (?), Source, Destination, Transaction Type,
    Inventory Value, User Name

"Stock Location" carries a question mark in their own document — whether bin
locations are in scope is open question Q9, so there is no field for it yet.
"""

from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator
from django.db import models
from django.db.models.functions import Lower


class MovementType(models.TextChoices):
    """Why stock moved. AsOne's "Transaction Type".

    Only RECEIPT is written today. The rest are named now because the ledger
    is append-only — a movement recorded under the wrong type cannot be
    edited later, so the vocabulary needs to be right before rows exist.
    """

    RECEIPT = "RECEIPT", "Receipt from a Tailoring Center"
    ADJUSTMENT = "ADJUSTMENT", "Inventory adjustment"
    TRANSFER_IN = "TRANSFER_IN", "Transfer in from another warehouse"
    TRANSFER_OUT = "TRANSFER_OUT", "Transfer out to another warehouse"
    PICK = "PICK", "Picked for a school order"
    SHIPMENT = "SHIPMENT", "Shipped to a school"
    RETURN = "RETURN", "Returned by a school"
    DAMAGE = "DAMAGE", "Damaged or lost"


class StockStatus(models.TextChoices):
    """Where stock sits in its life: Available -> Pick -> Shipped (p.8).

    Receipts land as AVAILABLE, which is unambiguous. The Pick and Shipped
    transitions depend on open question Q1 — AsOne's own chart says
    "Shipped ???" — so nothing here decides them.
    """

    AVAILABLE = "AVAILABLE", "Available"
    PICK = "PICK", "Picked"
    SHIPPED = "SHIPPED", "Shipped"


class StockMovement(models.Model):
    """One permanent, immutable row: this much of this SKU moved, here, then.

    `quantity` is **signed**. Positive is into the warehouse, negative is out.
    Summing the column therefore gives the stock level directly, with no
    per-type arithmetic that a future movement type could get wrong.
    """

    number = models.CharField(
        max_length=16,
        unique=True,
        editable=False,
        help_text="AsOne's Transaction #. System assigned, never reused.",
    )

    warehouse = models.ForeignKey(
        "catalog.Warehouse", on_delete=models.PROTECT, related_name="stock_movements"
    )
    sku = models.ForeignKey(
        "catalog.Sku", on_delete=models.PROTECT, related_name="stock_movements"
    )

    quantity = models.IntegerField(
        help_text="Signed: positive into the warehouse, negative out of it."
    )
    movement_type = models.CharField(max_length=16, choices=MovementType.choices)
    stock_status = models.CharField(
        max_length=12, choices=StockStatus.choices, default=StockStatus.AVAILABLE
    )

    # AsOne's "Inventory Value". Snapshotted from the document that caused the
    # movement, not looked up — the value of goods received in January is what
    # was paid in January, whatever the price list says in June.
    unit_value = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        help_text="Value per unit at the time of the movement. Never recalculated.",
    )

    # AsOne's "Document #": the receipt, adjustment or shipment that caused
    # this. A plain string rather than a foreign key, because the ledger
    # outlives any one document type and must not gain a nullable column per
    # app that ever writes to it.
    document_number = models.CharField(
        max_length=32, db_index=True, help_text="The document that caused this movement."
    )
    source = models.CharField(
        max_length=120, blank=True, help_text='Where it came from, e.g. "Idudi".'
    )
    destination = models.CharField(
        max_length=120, blank=True, help_text="Where it went, for outbound movements."
    )

    # Every transaction records the user (p.9). PROTECT, so the person who
    # posted a movement can never be erased from it.
    created_by = models.ForeignKey(
        "accounts.User", on_delete=models.PROTECT, related_name="stock_movements"
    )
    occurred_on = models.DateField(help_text="The date the movement actually happened.")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-occurred_on", "-id"]
        indexes = [
            # Stock levels are summed per SKU per warehouse — the one query
            # this table exists to answer.
            models.Index(fields=["warehouse", "sku", "stock_status"]),
            models.Index(fields=["sku", "-occurred_on"]),
        ]
        constraints = [
            # A movement of nothing is not a movement. It would sit in the
            # audit trail implying something happened when nothing did.
            models.CheckConstraint(
                condition=~models.Q(quantity=0), name="stock_movement_is_not_zero"
            ),
            models.CheckConstraint(
                condition=models.Q(unit_value__gte=0),
                name="stock_movement_value_is_not_negative",
            ),
        ]

    def __str__(self):
        direction = "+" if self.quantity > 0 else ""
        return f"{self.number} {direction}{self.quantity} {self.sku.number} @ {self.warehouse.name}"

    @property
    def total_value(self):
        return self.unit_value * abs(self.quantity)

    def save(self, *args, **kwargs):
        """Append only. An existing row can never be changed.

        Enforced here rather than left to convention, because the whole audit
        trail rests on it: a ledger you can edit is a ledger that cannot be
        trusted, and the edit would leave no trace of itself. Correct a
        mistake by posting an adjustment.
        """
        if self.pk is not None:
            raise ValidationError(
                "Stock movements are append-only and cannot be modified. "
                "Post an inventory adjustment instead."
            )

        if not self.number:
            from inventory.services import next_movement_number

            self.number = next_movement_number()

        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        """Never. Same reason as save()."""
        raise ValidationError(
            "Stock movements are permanent and cannot be deleted. "
            "Post a reversing inventory adjustment instead."
        )


class ReasonCode(models.Model):
    """Why an inventory adjustment was made — F13.

    AsOne's own words (p.6): "Reason Code - Database Table". The four they
    named on p.3 are Return, Warehouse Transfers, Pick up or Loss and
    Damaged, followed by "May be more…" — so this is a table Central Office
    maintains, not a fixed list in code. That is the whole reason it is a
    model rather than a `TextChoices`.

    Deliberately a plain lookup table. It carries **no financial behaviour**,
    because AsOne has not decided what that behaviour is: p.6 gives each
    adjustment kind a different financial note, and Damages is marked "To be
    determined" — open question Q6. Guessing now and building on the guess
    would be worse than waiting.

    It also carries no movement type. Every F23 adjustment posts as
    MovementType.ADJUSTMENT regardless of the code chosen — the reason code
    explains *why*, not the kind of ledger row it produces.

    Used by Phase 2 adjustments. F23 is the first thing that writes stock
    movements against a reason code — the table existed before that as
    Phase 1 master data, so Central Office could populate it ahead of need.
    """

    class AdjustmentDirection(models.TextChoices):
        """Whether posting an adjustment against this code adds to stock or
        takes away from it.

        Added for F23. The person posting an adjustment enters a plain,
        positive count of how many units changed — never a sign — and this
        is what turns that count into the signed figure the ledger actually
        stores. Without it, the same "Damaged" code could be posted as a
        gain by mistake, and nothing here would catch it.
        """

        INCREASE = "INCREASE", "Increases stock"
        DECREASE = "DECREASE", "Decreases stock"

    code = models.CharField(
        max_length=20,
        help_text='Short identifier, e.g. "DMG". Appears on adjustment documents.',
    )
    name = models.CharField(max_length=120, help_text='For example "Damaged in transit".')
    description = models.TextField(
        blank=True, help_text="When to use this code, for whoever is choosing one."
    )
    direction = models.CharField(
        max_length=8,
        choices=AdjustmentDirection.choices,
        help_text="Whether an adjustment posted against this code adds to stock or removes from it.",
    )

    # Same "Active Y/N" pattern as Garment and Sku. Codes are retired, never
    # deleted: past adjustments point at them, and an audit trail that cannot
    # say why a movement happened is not an audit trail.
    is_active = models.BooleanField(
        default=True,
        help_text="Inactive codes stay on past adjustments but cannot be chosen for new ones.",
    )

    class Meta:
        ordering = ["code"]
        verbose_name = "inventory adjustment reason code"
        constraints = [
            # Case-insensitive, like every other name in this project. "DMG"
            # and "dmg" are the same code to everyone except the database.
            models.UniqueConstraint(Lower("code"), name="unique_reason_code"),
            models.UniqueConstraint(Lower("name"), name="unique_reason_code_name"),
        ]

    def __str__(self):
        return f"{self.code} — {self.name}"


class WarehouseTransfer(models.Model):
    """Stock moving between the two warehouses — F25.

    **No money moves** (p.6). AsOne owns the stock either side, so a transfer
    changes where it is, not what it is worth. Posting writes two ledger rows
    per line at the same unit value: one out of the source, one into the
    destination. Total inventory value is identical before and after.

    Not to be confused with a **backorder transfer**, which is Phase 3 and a
    different thing entirely: there, a warehouse holding stock takes over
    another warehouse's backorder and ships **direct to the school** (decision
    D2). Goods never pass through the school's own warehouse. That is an
    outbound shipment, not this.

    Entering and posting are separate, as with receipts: a transfer can be
    prepared, checked, then committed.
    """

    number = models.CharField(
        max_length=16,
        unique=True,
        editable=False,
        help_text="System assigned. Unique forever, never reused.",
    )

    from_warehouse = models.ForeignKey(
        "catalog.Warehouse", on_delete=models.PROTECT, related_name="transfers_out"
    )
    to_warehouse = models.ForeignKey(
        "catalog.Warehouse", on_delete=models.PROTECT, related_name="transfers_in"
    )

    transfer_date = models.DateField(help_text="The date the stock actually moved.")

    # Optional: the reason codes table (F13) covers "Warehouse transfer", but a
    # transfer is a movement in its own right rather than an adjustment, so a
    # code is a note about *why* rather than what kind of movement this is.
    reason_code = models.ForeignKey(
        "inventory.ReasonCode",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="transfers",
        help_text="Optional. Why this rebalancing was needed.",
    )
    notes = models.TextField(blank=True)

    posted_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="When this transfer was posted to the ledger. Blank means not yet posted.",
    )
    created_by = models.ForeignKey(
        "accounts.User", on_delete=models.PROTECT, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-transfer_date", "-number"]
        constraints = [
            # Stock cannot move to where it already is. Allowing it would post
            # a matching pair of rows that cancel out — noise in the audit
            # trail implying something happened when nothing did.
            models.CheckConstraint(
                condition=~models.Q(from_warehouse=models.F("to_warehouse")),
                name="transfer_between_different_warehouses",
            )
        ]

    def __str__(self):
        return f"{self.number}: {self.from_warehouse} -> {self.to_warehouse}"

    @property
    def is_posted(self) -> bool:
        return self.posted_at is not None

    def clean(self):
        super().clean()
        if self.from_warehouse_id and self.from_warehouse_id == self.to_warehouse_id:
            raise ValidationError(
                {"to_warehouse": "A transfer must be between two different warehouses."}
            )

    def save(self, *args, **kwargs):
        if not self.number:
            from inventory.services import next_transfer_number

            self.number = next_transfer_number()
        super().save(*args, **kwargs)


class WarehouseTransferLine(models.Model):
    """One SKU on a transfer, and how many of it move."""

    transfer = models.ForeignKey(
        WarehouseTransfer, on_delete=models.CASCADE, related_name="lines"
    )
    sku = models.ForeignKey("catalog.Sku", on_delete=models.PROTECT, related_name="+")
    quantity = models.PositiveIntegerField(validators=[MinValueValidator(1)])

    # Snapshotted when the transfer is posted, from the value the stock
    # already carried at the source. Both ledger rows use it, which is what
    # makes "no money moves" true rather than merely intended.
    unit_value = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        null=True,
        blank=True,
        help_text="Set when the transfer is posted, from the value at the source warehouse.",
    )

    class Meta:
        ordering = ["sku__description"]
        constraints = [
            models.UniqueConstraint(
                fields=["transfer", "sku"], name="unique_sku_per_transfer"
            )
        ]

    def __str__(self):
        return f"{self.quantity} x {self.sku.number}"


class InventoryAdjustment(models.Model):
    """A quantity change against a reason code — F23.

    The generic shape the rest of Phase 2 reuses: physical count
    corrections, returns and damages are all this same document with a
    different reason code, so this is the one to get right first.

    One SKU per adjustment for now. Bashir, 28 August 2026: easy to widen to
    several SKUs later if one reason code ever needs to cover many at once —
    not built now because nothing has asked for it yet.

    Entering and posting are separate steps, same as WarehouseTransfer: an
    adjustment can be written down and checked before it actually changes
    what the system thinks is on the shelf.
    """

    number = models.CharField(
        max_length=16,
        unique=True,
        editable=False,
        help_text="System assigned. Unique forever, never reused.",
    )

    warehouse = models.ForeignKey(
        "catalog.Warehouse", on_delete=models.PROTECT, related_name="inventory_adjustments"
    )
    sku = models.ForeignKey(
        "catalog.Sku", on_delete=models.PROTECT, related_name="inventory_adjustments"
    )

    # Always entered as a plain positive count. The reason code decides
    # whether that increases or decreases stock — see
    # ReasonCode.AdjustmentDirection — so nobody has to remember which way a
    # given reason should move the number.
    quantity = models.PositiveIntegerField(
        validators=[MinValueValidator(1)],
        help_text="How many units changed. The reason code decides the direction.",
    )
    reason_code = models.ForeignKey(
        "inventory.ReasonCode",
        on_delete=models.PROTECT,
        related_name="adjustments",
        help_text="Why this adjustment was made. Also decides its direction.",
    )

    adjustment_date = models.DateField(
        help_text="The date the change actually happened, e.g. the day of a physical count."
    )

    # Set when posted, from the SKU's catalog price on adjustment_date — not
    # looked up again afterwards. Same reasoning as StockMovement.unit_value:
    # the value of a correction made in January is what the item was worth in
    # January, whatever the price list says later.
    unit_value = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        null=True,
        blank=True,
        help_text="Set when posted, from the SKU's catalog price on adjustment_date.",
    )
    notes = models.TextField(blank=True)

    posted_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="When this was posted to the ledger. Blank means not yet posted.",
    )
    created_by = models.ForeignKey(
        "accounts.User", on_delete=models.PROTECT, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-adjustment_date", "-number"]
        constraints = [
            # Mirrors stock_movement_is_not_zero: an adjustment of nothing
            # is not an adjustment.
            models.CheckConstraint(
                condition=models.Q(quantity__gt=0), name="adjustment_quantity_is_positive"
            ),
        ]

    def __str__(self):
        return f"{self.number}: {self.quantity} x {self.sku.number} ({self.reason_code.code})"

    @property
    def is_posted(self) -> bool:
        return self.posted_at is not None

    def save(self, *args, **kwargs):
        if not self.number:
            from inventory.services import next_adjustment_number

            self.number = next_adjustment_number()
        super().save(*args, **kwargs)
