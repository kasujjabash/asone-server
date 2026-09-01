"""Business logic for school orders.

Split by who does the work, not by layer:

    pos.py         a school places, invoices and cancels an order
    fulfilment.py  a warehouse checks stock, picks and reserves it

Everything is re-exported here, so callers import from `orders.services` and
never need to know which file something lives in. That also means the split
cost nothing at the call sites.
"""

from .fulfilment import (
    OrderCannotBePicked,
    OrderNotFillable,
    check_availability,
    pick_order,
)
from .pos import (
    CannotCancel,
    EmptyOrder,
    InactiveItem,
    WrongSchoolLevel,
    cancel_order,
    invoice_for,
    next_order_number,
    order_demand,
    orders_on_hold,
    place_order,
)

__all__ = [
    "CannotCancel",
    "EmptyOrder",
    "InactiveItem",
    "OrderCannotBePicked",
    "OrderNotFillable",
    "WrongSchoolLevel",
    "cancel_order",
    "check_availability",
    "invoice_for",
    "next_order_number",
    "order_demand",
    "orders_on_hold",
    "pick_order",
    "place_order",
]
