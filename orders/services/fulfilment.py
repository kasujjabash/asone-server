"""Filling a school order from a warehouse — F37, F38, F39.

Warehouse-facing, where the point of sale next door in pos.py is school-
facing. Split out because they are read and changed by different people, and
because one file holding both had grown past the ~300 lines this project
splits at.

Picking does not move stock out of the warehouse. It **recategorises** it:
out of Available, into Pick, at the same value. Total stock is unchanged;
what changes is how much of it is still free to promise to another order.
That is what reserving means in a ledger with no reservations table.

Moving stock out entirely is F41, and it is deliberately absent — the point
at which stock leaves is open question Q1, and AsOne's own chart says
"Shipped ???".
"""

from django.db import transaction
from django.utils import timezone

from ..models.school_orders import OrderStatus
from .pos import order_demand

# ---------------------------------------------------------------------------
# Warehouse fulfilment — F37, F38, F39
# ---------------------------------------------------------------------------


class OrderNotFillable(Exception):
    """Not enough stock to fill every line. Refused rather than picked
    partially — a pick list half completed is worse than one not started."""

    def __init__(self, shortfalls):
        self.shortfalls = shortfalls
        listed = ", ".join(
            f"{row['sku'].number} (need {row['needed']}, have {row['available']})"
            for row in shortfalls
        )
        super().__init__(f"Not enough stock to fill this order: {listed}.")


class OrderCannotBePicked(Exception):
    """The order's own status rules out picking it."""


#: Must an order be paid for before the warehouse may pick it?
#:
#: **This is a placeholder for open question Q2, not a decision.** AsOne's
#: chart puts payment before fulfilment, so the honest answer is almost
#: certainly True — but nothing could reach RELEASED when this code was
#: written, and requiring it would have made F39 unreachable.
#:
#: `release_order()` now exists, so flipping this to True is a one-line
#: change. It is left False because doing so silently is worse than leaving
#: it: turning it on refuses picking for every order placed before the
#: release step existed, including everything `seed_scenario` creates.
#:
#: To turn it on: set True, then decide what happens to orders already on
#: Hold. Both branches are covered in
#: `orders/tests/test_release.py::PickingAndTheReleaseGate`.
REQUIRE_RELEASE_BEFORE_PICK = False


def check_availability(order):
    """F37 — can the warehouse actually fill this order right now.

    One row per SKU: how many the order needs, how many are AVAILABLE at
    the order's warehouse, and the shortfall (0 when there is enough).

    pick_order() refuses using this exact comparison, so "can this be
    filled" (F37) and "fill it" (F39) can never disagree.
    """
    from inventory.services import stock_level

    warehouse = order.warehouse
    rows = []
    for sku, needed in order_demand(order):
        available = stock_level(sku, warehouse)
        rows.append(
            {
                "sku": sku,
                "needed": needed,
                "available": available,
                "shortfall": max(needed - available, 0),
            }
        )
    return rows


@transaction.atomic
def pick_order(order, *, picked_by):
    """Reserve stock for an order — F39: Available -> Pick.

    Posts two ledger rows per line, at the same value: one out of
    AVAILABLE, one into PICK. Total stock at the warehouse does not
    change — what changes is how much of it is still free to promise to a
    different order. That is what "reserving" means in a ledger with no
    separate reservations table: recategorise, the same shape a transfer
    uses between warehouses, here between statuses at one.

    Refused if the order is cancelled or already picked/shipped.

    Whether it is also refused for an order nobody has paid for is
    `REQUIRE_RELEASE_BEFORE_PICK` above — see that comment. It is off, which
    is the behaviour this function has always had.

    Refused, atomically, if any line is short — see OrderNotFillable and
    check_availability().
    """
    if order.status == OrderStatus.CANCELLED:
        raise OrderCannotBePicked(f"{order.number} is cancelled and cannot be picked.")
    if REQUIRE_RELEASE_BEFORE_PICK and order.status == OrderStatus.HOLD:
        raise OrderCannotBePicked(
            f"{order.number} has not been paid for. An order must be released "
            "before the warehouse picks it."
        )
    if order.status in (OrderStatus.PICKED, OrderStatus.SHIPPED):
        raise OrderCannotBePicked(f"{order.number} has already been picked.")

    from inventory.models import MovementType, StockStatus
    from inventory.services import average_unit_value, post_movement

    warehouse = order.warehouse
    rows = check_availability(order)
    short = [row for row in rows if row["shortfall"] > 0]
    if short:
        raise OrderNotFillable(short)

    picked_on = timezone.now().date()
    for row in rows:
        sku, quantity = row["sku"], row["needed"]
        # Recategorised at what the stock is already carried at, the same
        # reasoning average_unit_value() exists for on a transfer — picking
        # does not create or destroy value, only where it sits.
        unit_value = average_unit_value(sku, warehouse)

        post_movement(
            warehouse=warehouse,
            sku=sku,
            quantity=-quantity,
            movement_type=MovementType.PICK,
            stock_status=StockStatus.AVAILABLE,
            unit_value=unit_value,
            document_number=order.number,
            occurred_on=picked_on,
            created_by=picked_by,
        )
        post_movement(
            warehouse=warehouse,
            sku=sku,
            quantity=quantity,
            movement_type=MovementType.PICK,
            stock_status=StockStatus.PICK,
            unit_value=unit_value,
            document_number=order.number,
            occurred_on=picked_on,
            created_by=picked_by,
        )

    order.status = OrderStatus.PICKED
    order.save(update_fields=["status"])
    return order
