"""Reading and writing the stock ledger.

Everything that changes stock goes through `post_movement()`. Nothing else in
the project should construct a StockMovement directly — one entry point is
what makes "every movement records the user" a fact rather than a hope.

Reading is the other half: because there is no quantity column, a stock level
is a `Sum()` over the ledger. Written once here so that fifteen future callers
cannot each invent their own slightly different arithmetic.
"""

from decimal import Decimal

from decimal import Decimal

from django.db import connection, transaction
from django.utils import timezone
from django.db.models import DecimalField, F, IntegerField, Sum, Value
from django.db.models.functions import Coalesce

from .models import (
    InventoryAdjustment,
    MovementType,
    ReasonCode,
    StockMovement,
    StockStatus,
    WarehouseTransfer,
    WarehouseTransferLine,
)

#: Created by the initial migration. AsOne's "Transaction #".
MOVEMENT_SEQUENCE = "inventory_movement_seq"
#: Money always sums as a decimal — quantity is an integer and unit_value a
#: decimal, and Django will not guess what their product should be.
MONEY = DecimalField(max_digits=18, decimal_places=2)
TRANSFER_SEQUENCE = "inventory_transfer_seq"
ADJUSTMENT_SEQUENCE = "inventory_adjustment_seq"


def next_movement_number() -> str:
    """Draw the next transaction number.

    A Postgres sequence: `nextval` is atomic, so two warehouses posting at the
    same instant cannot collide, and it never goes backwards, so a number is
    never reused. Both matter for a document trail auditors read.
    """
    with connection.cursor() as cursor:
        cursor.execute("SELECT nextval(%s)", [MOVEMENT_SEQUENCE])
        return f"TX-{cursor.fetchone()[0]}"


def next_transfer_number() -> str:
    """The next warehouse transfer number. Same sequence guarantees as above."""
    with connection.cursor() as cursor:
        cursor.execute("SELECT nextval(%s)", [TRANSFER_SEQUENCE])
        return f"WT-{cursor.fetchone()[0]}"


def next_adjustment_number() -> str:
    """The next inventory adjustment number. Same sequence guarantees as above."""
    with connection.cursor() as cursor:
        cursor.execute("SELECT nextval(%s)", [ADJUSTMENT_SEQUENCE])
        return f"ADJ-{cursor.fetchone()[0]}"


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------


@transaction.atomic
def post_movement(
    *,
    warehouse,
    sku,
    quantity,
    movement_type,
    unit_value,
    document_number,
    occurred_on,
    created_by,
    stock_status=StockStatus.AVAILABLE,
    source="",
    destination="",
):
    """Write one permanent row to the ledger.

    `quantity` is signed — positive into the warehouse, negative out.

    The user is a required keyword argument rather than something looked up
    from a request, so a management command or an import cannot post
    anonymously by forgetting to pass it.
    """
    return StockMovement.objects.create(
        warehouse=warehouse,
        sku=sku,
        quantity=quantity,
        movement_type=movement_type,
        stock_status=stock_status,
        unit_value=unit_value,
        document_number=document_number,
        occurred_on=occurred_on,
        created_by=created_by,
        source=source,
        destination=destination,
    )


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------


def _ledger(warehouse=None, sku=None, as_of=None, stock_status=StockStatus.AVAILABLE):
    """The rows that count towards a stock level."""
    queryset = StockMovement.objects.all()

    if stock_status is not None:
        queryset = queryset.filter(stock_status=stock_status)
    if warehouse is not None:
        queryset = queryset.filter(warehouse=warehouse)
    if sku is not None:
        queryset = queryset.filter(sku=sku)
    if as_of is not None:
        # "As at" a date, so a count sheet from last Friday can be checked
        # against what the system thought last Friday.
        queryset = queryset.filter(occurred_on__lte=as_of)

    return queryset


def stock_level(sku, warehouse, as_of=None) -> int:
    """How many of ``sku`` are at ``warehouse``.

    Summed from the ledger, never read from a stored figure. Returns 0 when
    nothing has ever moved — which is the truthful answer, not a missing one.
    """
    return _ledger(warehouse=warehouse, sku=sku, as_of=as_of).aggregate(
        level=Coalesce(Sum("quantity"), Value(0), output_field=IntegerField())
    )["level"]


def stock_levels(warehouse=None, as_of=None, include_zero=False):
    """Stock levels for every SKU that has ever moved — F47.

    Returns rows of ``{sku_id, sku__number, sku__description, warehouse_id,
    warehouse__name, level, value}`` in one query, whatever the SKU count.

    Zero-level rows are excluded by default: a SKU that has moved in and back
    out again is not "stock", and a warehouse list should show what is there.
    """
    rows = (
        _ledger(warehouse=warehouse, as_of=as_of)
        .values("sku_id", "sku__number", "sku__description", "warehouse_id", "warehouse__name")
        .annotate(
            level=Coalesce(Sum("quantity"), Value(0), output_field=IntegerField()),
            # output_field is required: quantity is an integer and
            # unit_value a decimal, and Django will not guess which the
            # product should be. Decimal, obviously — this is money.
            value=Coalesce(
                Sum(
                    F("quantity") * F("unit_value"),
                    output_field=DecimalField(max_digits=16, decimal_places=2),
                ),
                Value(Decimal("0.00")),
                output_field=DecimalField(max_digits=16, decimal_places=2),
            ),
        )
        .order_by("warehouse__name", "sku__description")
    )

    return rows if include_zero else rows.filter(level__gt=0)


def below_minimum(warehouse=None, as_of=None):
    """SKUs at or under their reorder floor — F50.

    A floor set at a warehouse where the SKU has never moved still counts:
    zero is below any positive minimum, and that is exactly the case worth
    alerting on — stock that should be there and is not.
    """
    from catalog.models import MinimumStockLevel

    floors = MinimumStockLevel.objects.select_related("sku", "sku__garment", "warehouse")
    if warehouse is not None:
        floors = floors.filter(warehouse=warehouse)

    levels = {
        (row["warehouse_id"], row["sku_id"]): row["level"]
        for row in stock_levels(warehouse=warehouse, as_of=as_of, include_zero=True)
    }

    alerts = []
    for floor in floors:
        level = levels.get((floor.warehouse_id, floor.sku_id), 0)
        if level <= floor.minimum_quantity:
            alerts.append(
                {
                    "sku": floor.sku,
                    "warehouse": floor.warehouse,
                    "level": level,
                    "minimum": floor.minimum_quantity,
                    "shortfall": floor.minimum_quantity - level,
                }
            )

    return sorted(alerts, key=lambda a: (a["warehouse"].name, a["sku"].description))


def movements_for_sku(sku, warehouse=None):
    """The audit trail for one SKU — F48.

    Every movement, newest first, with the user who posted it. This is the
    question a ledger exists to answer and a counter cannot.
    """
    queryset = StockMovement.objects.filter(sku=sku).select_related(
        "warehouse", "created_by"
    )
    if warehouse is not None:
        queryset = queryset.filter(warehouse=warehouse)
    return queryset.order_by("-occurred_on", "-id")


# ---------------------------------------------------------------------------
# Warehouse transfers — F25
# ---------------------------------------------------------------------------


class TransferAlreadyPosted(Exception):
    """Posting twice would move the stock twice."""


class NotEnoughStock(Exception):
    """The source warehouse does not hold what the transfer is trying to move.

    Refused rather than allowed to go negative. Negative stock is not a
    number anyone can act on — it means either the count is wrong or the
    ledger is, and posting a transfer on top would bury which.
    """

    def __init__(self, shortfalls):
        self.shortfalls = shortfalls
        listed = ", ".join(
            f"{s['sku'].number} (moving {s['requested']}, only {s['available']} on hand)"
            for s in shortfalls
        )
        super().__init__(f"Not enough stock at the source warehouse: {listed}.")


def average_unit_value(sku, warehouse, as_of=None):
    """What one unit of ``sku`` at ``warehouse`` is currently carried at.

    Total value divided by units on hand, taken from the ledger. Used to
    value a transfer so the same figure leaves one warehouse and arrives at
    the other — which is what makes "no money moves" (p.6) true rather than
    merely intended.

    Returns None when nothing is on hand; there is no value to carry across,
    and the transfer will be refused for lack of stock anyway.
    """
    row = _ledger(warehouse=warehouse, sku=sku, as_of=as_of).aggregate(
        level=Coalesce(Sum("quantity"), Value(0), output_field=IntegerField()),
        value=Coalesce(
            Sum(F("quantity") * F("unit_value"), output_field=MONEY),
            Value(Decimal("0.00")),
            output_field=MONEY,
        ),
    )

    if not row["level"]:
        return None
    return (row["value"] / row["level"]).quantize(Decimal("0.01"))


@transaction.atomic
def create_transfer(*, from_warehouse, to_warehouse, lines, created_by, **fields):
    """Prepare a transfer without moving anything.

    Entering and posting are separate, as with receipts: a transfer can be
    written down, checked against what is actually on the shelf, and only
    then committed.

    Stock is checked here as well as at posting. Catching it now tells
    someone before they load a van; the check at posting is what actually
    guarantees it, because stock can move in between.
    """
    lines = list(lines)
    if not lines:
        raise ValueError("A transfer needs at least one line.")

    _refuse_if_short(from_warehouse, lines)

    transfer = WarehouseTransfer(
        from_warehouse=from_warehouse,
        to_warehouse=to_warehouse,
        created_by=created_by,
        **fields,
    )
    transfer.full_clean(exclude=["number", "created_by"])
    transfer.save()

    WarehouseTransferLine.objects.bulk_create(
        [
            WarehouseTransferLine(
                transfer=transfer, sku=line["sku"], quantity=line["quantity"]
            )
            for line in lines
        ]
    )
    return transfer


def _refuse_if_short(warehouse, lines, as_of=None):
    """Check every line against what the source actually holds."""
    shortfalls = []
    for line in lines:
        sku = line["sku"] if isinstance(line, dict) else line.sku
        wanted = line["quantity"] if isinstance(line, dict) else line.quantity

        available = stock_level(sku, warehouse, as_of=as_of)
        if wanted > available:
            shortfalls.append(
                {"sku": sku, "requested": wanted, "available": available}
            )

    if shortfalls:
        raise NotEnoughStock(shortfalls)


@transaction.atomic
def post_transfer(transfer, *, posted_by):
    """Commit a transfer: two ledger rows per line, out and in.

    Atomic, and it has to be. A transfer half-posted would take stock out of
    one warehouse without putting it into the other — the goods would simply
    cease to exist, and no later count could say where they went.

    Both rows carry the same unit value, taken from what the stock was
    already carried at. Total inventory value across the two warehouses is
    unchanged, which is what p.6 means by "no money moves".

    Stock is re-checked here even though create_transfer checked it: time
    passes between writing a transfer down and committing it, and something
    else may have moved the stock.
    """
    if transfer.is_posted:
        raise TransferAlreadyPosted(
            f"{transfer.number} was already posted on {transfer.posted_at:%Y-%m-%d}."
        )

    lines = list(transfer.lines.select_related("sku"))
    _refuse_if_short(transfer.from_warehouse, lines, as_of=transfer.transfer_date)

    movements = []
    for line in lines:
        unit_value = average_unit_value(
            line.sku, transfer.from_warehouse, as_of=transfer.transfer_date
        )

        common = {
            "sku": line.sku,
            "unit_value": unit_value,
            "document_number": transfer.number,
            "occurred_on": transfer.transfer_date,
            "created_by": posted_by,
            "source": transfer.from_warehouse.name,
            "destination": transfer.to_warehouse.name,
        }
        movements.append(
            post_movement(
                warehouse=transfer.from_warehouse,
                quantity=-line.quantity,
                movement_type=MovementType.TRANSFER_OUT,
                **common,
            )
        )
        movements.append(
            post_movement(
                warehouse=transfer.to_warehouse,
                quantity=line.quantity,
                movement_type=MovementType.TRANSFER_IN,
                **common,
            )
        )

        line.unit_value = unit_value
        line.save(update_fields=["unit_value"])

    transfer.posted_at = timezone.now()
    transfer.save(update_fields=["posted_at"])

    return movements


# ---------------------------------------------------------------------------
# Inventory adjustments — F23
# ---------------------------------------------------------------------------


class AdjustmentAlreadyPosted(Exception):
    """Posting twice would move the stock twice."""


@transaction.atomic
def create_adjustment(*, warehouse, sku, quantity, reason_code, created_by, adjustment_date, **fields):
    """Prepare an inventory adjustment without touching the ledger.

    Entering and posting are separate steps, same as a warehouse transfer: an
    adjustment can be written down and checked before it actually changes
    what the system thinks is on the shelf.

    Refused up front if the SKU has no catalog price on ``adjustment_date`` —
    `PriceNotSet` propagates rather than being caught here, so an adjustment
    that could never be valued is never even written down. Checked again at
    posting, because a reprice can happen in between.

    Also refused up front if the reason code decreases stock and there is
    not enough on hand to take it from — `NotEnoughStock` propagates, same
    exception a transfer raises for the same reason. Negative stock is not a
    number anyone can act on: it means either the count or the ledger is
    wrong, and writing an adjustment on top would only bury which. An
    increase has no such limit, so this only runs for DECREASE codes.
    """
    from catalog.services import price_for_sku

    price_for_sku(sku, adjustment_date)

    if reason_code.direction == ReasonCode.AdjustmentDirection.DECREASE:
        _refuse_if_short(warehouse, [{"sku": sku, "quantity": quantity}], as_of=adjustment_date)

    adjustment = InventoryAdjustment(
        warehouse=warehouse,
        sku=sku,
        quantity=quantity,
        reason_code=reason_code,
        adjustment_date=adjustment_date,
        created_by=created_by,
        **fields,
    )
    adjustment.full_clean(exclude=["number", "created_by", "unit_value"])
    adjustment.save()
    return adjustment


@transaction.atomic
def post_adjustment(adjustment, *, posted_by):
    """Commit an adjustment: one permanent ledger row.

    The reason code decides the sign. `adjustment.quantity` is always a
    plain positive count — this is the one place it becomes the signed
    figure the ledger stores, by reading `reason_code.direction`. The person
    posting it is never asked to remember which way "Damaged" should move
    the number.

    Value comes from the SKU's catalog price on `adjustment_date`, looked up
    again here rather than reused from `create_adjustment()` — a reprice can
    happen between writing an adjustment down and posting it, and the price
    that actually applied on the day is the one this movement should carry.
    Unlike a transfer, this does **not** use `average_unit_value()`: an
    adjustment is not moving stock that already has a value carried on the
    ledger, it is correcting the count against what AsOne actually charges
    for the item.

    Raises `AdjustmentAlreadyPosted` if this has already run once, and lets
    `catalog.services.PriceNotSet` propagate if the SKU's price was removed
    since the adjustment was created.

    Stock is re-checked here too, for a DECREASE code, even though
    create_adjustment() already checked it — time passes between writing an
    adjustment down and posting it, and something else may have moved the
    stock in between. Same reasoning as post_transfer() re-checking
    NotEnoughStock rather than trusting the check from create_transfer().
    """
    if adjustment.is_posted:
        raise AdjustmentAlreadyPosted(
            f"{adjustment.number} was already posted on {adjustment.posted_at:%Y-%m-%d}."
        )

    if adjustment.reason_code.direction == ReasonCode.AdjustmentDirection.DECREASE:
        _refuse_if_short(
            adjustment.warehouse,
            [{"sku": adjustment.sku, "quantity": adjustment.quantity}],
            as_of=adjustment.adjustment_date,
        )

    from catalog.services import price_for_sku

    unit_value = price_for_sku(adjustment.sku, adjustment.adjustment_date)

    signed_quantity = adjustment.quantity
    if adjustment.reason_code.direction == ReasonCode.AdjustmentDirection.DECREASE:
        signed_quantity = -signed_quantity

    movement = post_movement(
        warehouse=adjustment.warehouse,
        sku=adjustment.sku,
        quantity=signed_quantity,
        movement_type=MovementType.ADJUSTMENT,
        unit_value=unit_value,
        document_number=adjustment.number,
        occurred_on=adjustment.adjustment_date,
        created_by=posted_by,
    )

    adjustment.unit_value = unit_value
    adjustment.posted_at = timezone.now()
    adjustment.save(update_fields=["unit_value", "posted_at"])

    return movement
