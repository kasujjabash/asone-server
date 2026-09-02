"""Business logic for school orders.

Split by who does the work, not by layer:

    pos.py         a school places, releases, invoices and cancels an order
    fulfilment.py  a warehouse checks stock, picks and reserves it
    shipping.py    a warehouse sends it out, and stock leaves
    backorders.py  what one warehouse could not supply, and who fills it

Everything is re-exported here, so callers import from `orders.services` and
never need to know which file something lives in. That also means the split
cost nothing at the call sites.
"""

from .fulfilment import (
    REQUIRE_RELEASE_BEFORE_PICK,
    OrderCannotBePicked,
    OrderNotFillable,
    check_availability,
    pick_order,
)
from .backorders import (
    CannotAssign,
    NoStockToFill,
    NothingToPick,
    assign_backorder,
    assigned_to,
    fill_backorder,
    open_backorders,
    pick_available,
    warehouses_that_could_fill,
)
from .shipping import (
    NothingToShip,
    OrderCannotBeShipped,
    next_shipment_number,
    packing_list_for,
    picked_stock_for,
    ship_order,
)
from .pos import (
    CannotCancel,
    CannotRelease,
    EmptyOrder,
    InactiveItem,
    WrongSchoolLevel,
    cancel_order,
    invoice_for,
    release_order,
    next_order_number,
    order_demand,
    orders_on_hold,
    place_order,
)

__all__ = [
    "REQUIRE_RELEASE_BEFORE_PICK",
    "CannotCancel",
    "CannotRelease",
    "EmptyOrder",
    "InactiveItem",
    "OrderCannotBePicked",
    "CannotAssign",
    "NoStockToFill",
    "NothingToPick",
    "NothingToShip",
    "OrderCannotBeShipped",
    "OrderNotFillable",
    "WrongSchoolLevel",
    "cancel_order",
    "check_availability",
    "invoice_for",
    "next_order_number",
    "order_demand",
    "orders_on_hold",
    "pick_order",
    "assign_backorder",
    "assigned_to",
    "fill_backorder",
    "next_shipment_number",
    "open_backorders",
    "packing_list_for",
    "pick_available",
    "warehouses_that_could_fill",
    "picked_stock_for",
    "place_order",
    "release_order",
    "ship_order",
]
