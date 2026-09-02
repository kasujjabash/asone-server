"""School orders — the point of sale.

Phase 3, and the largest phase: 23 of the 63 catalogued features. This is
where AsOne's stock actually reaches a student.

    school_orders.py   the order a school places, and its lines
    shipments.py       what physically left a warehouse for that order
    backorders.py      what a school is still owed, and who is filling it

The vocabulary is AsOne's, from p.7:

    School Order   what a school places for one student
    Invoice        the priced document the school gives the student
    Hold           the order's state until payment is confirmed
    Uniform Kit    a bundle a school may order as one line, which the
                   warehouse picks as individual SKUs
    Shipment       what left a warehouse — not always the school's own,
                   because a backorder may ship direct from another (D2)
"""

from .backorders import Backorder, BackorderStatus
from .school_orders import SchoolOrder, SchoolOrderLine
from .shipments import Shipment, ShipmentLine

__all__ = [
    "Backorder",
    "BackorderStatus",
    "SchoolOrder",
    "SchoolOrderLine",
    "Shipment",
    "ShipmentLine",
]
