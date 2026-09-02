"""Backorders — F43, F44, F45, F46.

What a school's own warehouse could not supply, and how another one fills it.

## The rule this serves

Decision D2, Jim on 24 August:

> "The warehouses should have the capability to transfer 'Backorders' to
> another warehouse with Inventory. The fulfilling warehouse will then ship
> directly to the appropriate school."

Two halves, and they must not be collapsed. **Ordering** is fixed to the
school's primary warehouse. **Fulfilment** may come from anywhere with stock,
and goes straight to the school — it does not route back through the school's
own warehouse first.

## Why picking short is opt-in

`pick_order()` has always refused an order it cannot fill completely, and
that refusal is right for the ordinary case: a clerk who asked for a pick
list wants to know before they walk to the shelf. Backorders make partial
picking meaningful, but turning it on by default would change what every
existing caller does — including `seed_scenario` and Denis's F39 tests.

So `pick_available()` is a separate entry point. `pick_order()` is untouched.
"""

from django.db import transaction
from django.utils import timezone

from ..models import Backorder
from ..models.backorders import BackorderStatus
from ..models.school_orders import OrderStatus
from .fulfilment import check_availability


class CannotAssign(Exception):
    """The backorder is not open, so it cannot be handed to a warehouse."""


class NoStockToFill(Exception):
    """The warehouse offered to fill it does not hold enough."""


class NothingToPick(Exception):
    """Not one unit of the order is available, so there is no pick to make."""


@transaction.atomic
def pick_available(order, *, picked_by):
    """Pick what the warehouse has, and record the rest as backorders — F43.

    The partial-fulfilment counterpart to `pick_order()`, which refuses an
    order it cannot fill in full. Both exist because they answer different
    questions: "can I fill this?" and "fill what you can, we will chase the
    rest".

    Returns ``(order, backorders)``.

    Refused if nothing at all is available — that is not a partial pick, it
    is an order the warehouse cannot start, and writing a backorder for the
    whole thing while marking the order Picked would be a lie.
    """
    from inventory.models import MovementType, StockStatus
    from inventory.services import average_unit_value, post_movement

    if order.status == OrderStatus.CANCELLED:
        raise NothingToPick(f"{order.number} is cancelled.")
    if order.status in (OrderStatus.PICKED, OrderStatus.SHIPPED):
        raise NothingToPick(f"{order.number} has already been picked.")

    warehouse = order.warehouse
    rows = check_availability(order)

    fillable = [row for row in rows if min(row["needed"], row["available"]) > 0]
    if not fillable:
        raise NothingToPick(
            f"{warehouse.name} holds none of what {order.number} needs, so "
            "there is nothing to pick. Assign the whole order to another "
            "warehouse instead."
        )

    picked_on = timezone.now().date()

    for row in fillable:
        sku = row["sku"]
        quantity = min(row["needed"], row["available"])
        unit_value = average_unit_value(sku, warehouse)

        post_movement(
            warehouse=warehouse, sku=sku, quantity=-quantity,
            movement_type=MovementType.PICK, stock_status=StockStatus.AVAILABLE,
            unit_value=unit_value, document_number=order.number,
            occurred_on=picked_on, created_by=picked_by,
        )
        post_movement(
            warehouse=warehouse, sku=sku, quantity=quantity,
            movement_type=MovementType.PICK, stock_status=StockStatus.PICK,
            unit_value=unit_value, document_number=order.number,
            occurred_on=picked_on, created_by=picked_by,
        )

    backorders = [
        Backorder(
            order=order,
            sku=row["sku"],
            quantity=row["shortfall"],
            created_by=picked_by,
            notes=f"{warehouse.name} was short at picking.",
        )
        for row in rows
        if row["shortfall"] > 0
    ]
    Backorder.objects.bulk_create(backorders)

    order.status = OrderStatus.PICKED
    order.save(update_fields=["status"])
    return order, backorders


def open_backorders(warehouse=None):
    """What is still owed — F44.

    ``warehouse`` narrows to backorders **raised by** that warehouse: the
    ones it could not supply. Not the ones it has agreed to fill; that is
    `assigned_to()`.
    """
    queryset = Backorder.objects.filter(
        status=BackorderStatus.OPEN
    ).select_related("order", "order__school", "sku", "sku__garment")

    if warehouse is not None:
        queryset = queryset.filter(order__school__primary_warehouse=warehouse)
    return queryset


def assigned_to(warehouse):
    """Backorders another warehouse has handed to ``warehouse`` to fill."""
    return Backorder.objects.filter(
        filled_by_warehouse=warehouse, status=BackorderStatus.ASSIGNED
    ).select_related("order", "order__school", "sku", "sku__garment")


def warehouses_that_could_fill(backorder):
    """Which warehouses hold enough to fill this — F45's shortlist.

    The screen that offers a transfer needs somewhere to send it, and asking
    a clerk to guess which warehouse has stock is how a backorder gets sent
    to one that does not.
    """
    from catalog.models import Warehouse
    from inventory.services import stock_level

    origin = backorder.origin_warehouse
    return [
        warehouse
        for warehouse in Warehouse.objects.exclude(pk=origin.pk)
        if stock_level(backorder.sku, warehouse) >= backorder.quantity
    ]


@transaction.atomic
def assign_backorder(backorder, *, warehouse, assigned_by):
    """Hand a backorder to a warehouse that has the stock — F45.

    The transfer AsOne asked for. Nothing moves in the ledger here: the
    receiving warehouse still has its stock, and will pick and ship it in
    the ordinary way. What changes is who owes the school.

    Refused if that warehouse does not actually hold enough. Sending a
    backorder to an empty warehouse produces a queue nobody can clear, and
    the clerk sending it cannot see the other site's shelves.
    """
    from inventory.services import stock_level

    if not backorder.can_be_assigned:
        raise CannotAssign(
            f"That backorder is {backorder.get_status_display().lower()} and "
            "cannot be reassigned."
        )

    if warehouse == backorder.origin_warehouse:
        raise CannotAssign(
            f"{warehouse.name} is the warehouse that ran short. A backorder "
            "has to go somewhere that has the stock."
        )

    available = stock_level(backorder.sku, warehouse)
    if available < backorder.quantity:
        raise NoStockToFill(
            f"{warehouse.name} holds {available} of {backorder.sku.number} "
            f"and the backorder needs {backorder.quantity}."
        )

    backorder.status = BackorderStatus.ASSIGNED
    backorder.filled_by_warehouse = warehouse
    backorder.assigned_at = timezone.now()
    backorder.assigned_by = assigned_by
    backorder.save(
        update_fields=["status", "filled_by_warehouse", "assigned_at", "assigned_by"]
    )
    return backorder


@transaction.atomic
def fill_backorder(backorder, *, filled_by, shipped_on=None, waybill_number="", notes=""):
    """The assigned warehouse picks and ships direct to the school — F46.

    This is the half of D2 that overrides the definitions page: the stock
    does **not** route back through the school's own warehouse. It goes from
    the shelf that had it, straight to the school.

    Reserves and ships in one step, because there is nothing between them
    for the fulfilling warehouse to decide — it already accepted the job
    when the backorder was assigned to it.

    Returns the Shipment.
    """
    from inventory.models import MovementType, StockStatus
    from inventory.services import average_unit_value, post_movement, stock_level

    from ..models import Shipment, ShipmentLine

    if backorder.status != BackorderStatus.ASSIGNED:
        raise CannotAssign(
            f"That backorder is {backorder.get_status_display().lower()}. Only "
            "an assigned backorder can be filled."
        )

    warehouse = backorder.filled_by_warehouse
    sku, quantity = backorder.sku, backorder.quantity

    available = stock_level(sku, warehouse)
    if available < quantity:
        raise NoStockToFill(
            f"{warehouse.name} now holds only {available} of {sku.number}. "
            "Something else took the stock since this was assigned."
        )

    shipped_on = shipped_on or timezone.now().date()
    unit_value = average_unit_value(sku, warehouse)

    shipment = Shipment.objects.create(
        order=backorder.order,
        from_warehouse=warehouse,
        shipped_on=shipped_on,
        shipped_by=filled_by,
        waybill_number=waybill_number.strip(),
        notes=notes or f"Backorder filled direct from {warehouse.name} (D2).",
    )

    # Straight out of AVAILABLE into SHIPPED. There is no PICK step: the
    # stock was never reserved here, and inventing a reservation it held for
    # no time would put two rows in the ledger that say nothing.
    post_movement(
        warehouse=warehouse, sku=sku, quantity=-quantity,
        movement_type=MovementType.SHIPMENT, stock_status=StockStatus.AVAILABLE,
        unit_value=unit_value, document_number=shipment.number,
        occurred_on=shipped_on, created_by=filled_by,
    )
    post_movement(
        warehouse=warehouse, sku=sku, quantity=quantity,
        movement_type=MovementType.SHIPMENT, stock_status=StockStatus.SHIPPED,
        unit_value=unit_value, document_number=shipment.number,
        occurred_on=shipped_on, created_by=filled_by,
    )

    ShipmentLine.objects.create(shipment=shipment, sku=sku, quantity=quantity)

    backorder.status = BackorderStatus.FILLED
    backorder.save(update_fields=["status"])
    return shipment
