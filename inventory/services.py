"""Reading and writing the stock ledger.

Everything that changes stock goes through `post_movement()`. Nothing else in
the project should construct a StockMovement directly — one entry point is
what makes "every movement records the user" a fact rather than a hope.

Reading is the other half: because there is no quantity column, a stock level
is a `Sum()` over the ledger. Written once here so that fifteen future callers
cannot each invent their own slightly different arithmetic.
"""

from decimal import Decimal

from django.db import connection, transaction
from django.db.models import DecimalField, F, IntegerField, Sum, Value
from django.db.models.functions import Coalesce

from .models import StockMovement, StockStatus

#: Created by the initial migration. AsOne's "Transaction #".
MOVEMENT_SEQUENCE = "inventory_movement_seq"


def next_movement_number() -> str:
    """Draw the next transaction number.

    A Postgres sequence: `nextval` is atomic, so two warehouses posting at the
    same instant cannot collide, and it never goes backwards, so a number is
    never reused. Both matter for a document trail auditors read.
    """
    with connection.cursor() as cursor:
        cursor.execute("SELECT nextval(%s)", [MOVEMENT_SEQUENCE])
        return f"TX-{cursor.fetchone()[0]}"


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
