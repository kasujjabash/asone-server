"""Getting a picked order out of the warehouse — F41.

Picking reserved the stock; this is where it leaves. Two ledger rows per
line at the same value: out of PICK, into SHIPPED. Total stock at the
warehouse falls, which is the point — it has gone.

## What AsOne has not decided, and where it lives

Their chart reads "Inventory moves from a 'Pick' status to 'Shipped' ???".
The question marks are theirs.

What the ledger already forced us to settle: stock is **committed at pick**
and **leaves at ship**. What is still open is which real-world event sets
Shipped — the van being loaded, or the school confirming it arrived.

`ship_order()` is written for the first, because that is what a warehouse
can actually observe: a clerk knows when a van left, and cannot know when it
arrived. If AsOne says arrival is what counts, the fix is a second field
(`received_at`, set by the school) and a second endpoint — **not** a change
to when the ledger moves. Moving the ledger on arrival would mean stock the
warehouse has physically given away still counting as theirs for days.

That is the recommendation to put to them, not a decision made for them.
"""

from django.db import connection, transaction
from django.utils import timezone

from ..models import Shipment, ShipmentLine
from ..models.school_orders import OrderStatus, SchoolOrder

SHIPMENT_SEQUENCE = "orders_shipment_seq"


def next_shipment_number() -> str:
    """The next shipment number — a Postgres sequence, never reused."""
    with connection.cursor() as cursor:
        cursor.execute("SELECT nextval(%s)", [SHIPMENT_SEQUENCE])
        return f"SH-{cursor.fetchone()[0]}"


class OrderCannotBeShipped(Exception):
    """The order is not in a state where anything can leave the warehouse."""


class NothingToShip(Exception):
    """No stock is reserved for this order at that warehouse."""


def picked_stock_for(order, warehouse):
    """What is currently reserved for ``order`` at ``warehouse``, per SKU.

    Read from the ledger rather than from the order's lines, because those
    are two different questions. The lines say what the school asked for;
    this says what is actually sitting in PICK with this order's number on
    it — which is less, whenever a pick was short.
    """
    from django.db.models import Sum

    from inventory.models import StockMovement, StockStatus

    rows = (
        StockMovement.objects.filter(
            document_number=order.number,
            warehouse=warehouse,
            stock_status=StockStatus.PICK,
        )
        .values("sku")
        .annotate(reserved=Sum("quantity"))
        .filter(reserved__gt=0)
    )
    return {row["sku"]: row["reserved"] for row in rows}


@transaction.atomic
def ship_order(order, *, shipped_by, from_warehouse=None, shipped_on=None,
               waybill_number="", notes=""):
    """Send a picked order out — F41.

    Moves every reserved unit from PICK to SHIPPED and records a Shipment
    saying what left, from where, and when.

    ``from_warehouse`` defaults to the order's own warehouse but is a
    parameter, not a derivation: decision D2 says a backorder may be filled
    by a different warehouse shipping direct to the school. Passing it
    explicitly is how that case is served.

    Refused if the order was never picked, is cancelled, or has already
    shipped. Refused too if nothing is actually reserved at that warehouse,
    which is the case that would otherwise write an empty shipment.
    """
    if order.status == OrderStatus.CANCELLED:
        raise OrderCannotBeShipped(f"{order.number} is cancelled.")
    if order.status == OrderStatus.SHIPPED:
        raise OrderCannotBeShipped(f"{order.number} has already been shipped.")
    if order.status != OrderStatus.PICKED:
        raise OrderCannotBeShipped(
            f"{order.number} is {order.get_status_display().lower()}. Only a "
            "picked order can be shipped."
        )

    from inventory.models import MovementType, StockStatus
    from inventory.services import average_unit_value, post_movement

    warehouse = from_warehouse or order.warehouse
    reserved = picked_stock_for(order, warehouse)
    if not reserved:
        raise NothingToShip(
            f"Nothing is reserved for {order.number} at {warehouse.name}, so "
            "there is nothing to ship."
        )

    shipped_on = shipped_on or timezone.now().date()

    shipment = Shipment.objects.create(
        school=order.school,
        from_warehouse=warehouse,
        shipped_on=shipped_on,
        shipped_by=shipped_by,
        waybill_number=waybill_number.strip(),
        notes=notes,
    )

    from catalog.models import Sku

    skus = Sku.objects.in_bulk(reserved.keys())
    lines = []
    for sku_id, quantity in reserved.items():
        sku = skus[sku_id]
        # Valued at what the stock is already carried at, the same reasoning
        # a transfer and a pick both use: moving stock does not create or
        # destroy value, only where it sits.
        unit_value = average_unit_value(sku, warehouse, stock_status=StockStatus.PICK)

        post_movement(
            warehouse=warehouse,
            sku=sku,
            quantity=-quantity,
            movement_type=MovementType.SHIPMENT,
            stock_status=StockStatus.PICK,
            unit_value=unit_value,
            document_number=shipment.number,
            occurred_on=shipped_on,
            created_by=shipped_by,
        )
        post_movement(
            warehouse=warehouse,
            sku=sku,
            quantity=quantity,
            movement_type=MovementType.SHIPMENT,
            stock_status=StockStatus.SHIPPED,
            unit_value=unit_value,
            document_number=shipment.number,
            occurred_on=shipped_on,
            created_by=shipped_by,
        )
        lines.append(
            ShipmentLine(shipment=shipment, order=order, sku=sku, quantity=quantity)
        )

    ShipmentLine.objects.bulk_create(lines)

    order.status = OrderStatus.SHIPPED
    order.save(update_fields=["status"])
    return shipment


class NothingReadyToDespatch(Exception):
    """No picked order at that warehouse is waiting for that school."""


@transaction.atomic
def despatch_to_school(
    *,
    school,
    from_warehouse,
    shipped_by,
    orders=None,
    shipped_on=None,
    waybill_number="",
    carrier_method="",
    notes="",
):
    """F42 — one consolidated despatch carrying a school's picked orders.

    AsOne's checklist: *"Consolidated weekly despatch; the school distributes
    to students by name on the packing list."* A van going to St Mary's
    carries every St Mary's order that is ready, and the packing list says
    whose each parcel is.

    ``orders`` narrows it to a chosen few; omitted, every picked order for
    that school at that warehouse goes. Either way each order is checked the
    same way `ship_order` checks one — a cancelled or unpicked order is
    refused rather than quietly left behind, because a clerk who asked for it
    to go needs to know it did not.

    **Stock moves per order, not per van.** The ledger rows are the same rows
    `ship_order` writes; consolidating is a fact about the document, not
    about the stock. That is what keeps a consolidated despatch and a single
    one worth exactly the same to Finance.
    """
    from inventory.models import MovementType, StockStatus
    from inventory.services import average_unit_value, post_movement
    from catalog.models import Sku

    if orders is None:
        orders = list(
            school.orders.filter(status=OrderStatus.PICKED).order_by("number")
        )
    else:
        orders = list(orders)
        wrong_school = [o.number for o in orders if o.school_id != school.id]
        if wrong_school:
            raise OrderCannotBeShipped(
                "A shipment goes to one school. These belong elsewhere: "
                + ", ".join(sorted(wrong_school))
                + "."
            )
        for order in orders:
            if order.status == OrderStatus.CANCELLED:
                raise OrderCannotBeShipped(f"{order.number} is cancelled.")
            if order.status == OrderStatus.SHIPPED:
                raise OrderCannotBeShipped(f"{order.number} has already been shipped.")
            if order.status != OrderStatus.PICKED:
                raise OrderCannotBeShipped(
                    f"{order.number} is {order.get_status_display().lower()}. "
                    "Only a picked order can be shipped."
                )

    if not orders:
        raise NothingReadyToDespatch(
            f"No picked order for {school.name} is waiting at {from_warehouse.name}."
        )

    # What is actually reserved, per order. An order with nothing reserved at
    # this warehouse is not on this van — its stock is somewhere else (D2).
    reserved_by_order = {}
    for order in orders:
        reserved = picked_stock_for(order, from_warehouse)
        if reserved:
            reserved_by_order[order] = reserved

    if not reserved_by_order:
        raise NothingToShip(
            f"Nothing is reserved for {school.name} at {from_warehouse.name}, "
            "so there is nothing to ship."
        )

    shipped_on = shipped_on or timezone.now().date()

    shipment = Shipment.objects.create(
        school=school,
        from_warehouse=from_warehouse,
        shipped_on=shipped_on,
        shipped_by=shipped_by,
        waybill_number=waybill_number.strip(),
        carrier_method=carrier_method.strip(),
        notes=notes,
    )

    sku_ids = {sku_id for r in reserved_by_order.values() for sku_id in r}
    skus = Sku.objects.in_bulk(sku_ids)

    lines = []
    for order, reserved in reserved_by_order.items():
        for sku_id, quantity in reserved.items():
            sku = skus[sku_id]
            unit_value = average_unit_value(
                sku, from_warehouse, stock_status=StockStatus.PICK
            )

            post_movement(
                warehouse=from_warehouse,
                sku=sku,
                quantity=-quantity,
                movement_type=MovementType.SHIPMENT,
                stock_status=StockStatus.PICK,
                unit_value=unit_value,
                document_number=shipment.number,
                occurred_on=shipped_on,
                created_by=shipped_by,
            )
            post_movement(
                warehouse=from_warehouse,
                sku=sku,
                quantity=quantity,
                movement_type=MovementType.SHIPMENT,
                stock_status=StockStatus.SHIPPED,
                unit_value=unit_value,
                document_number=shipment.number,
                occurred_on=shipped_on,
                created_by=shipped_by,
            )
            lines.append(
                ShipmentLine(
                    shipment=shipment, order=order, sku=sku, quantity=quantity
                )
            )

        order.status = OrderStatus.SHIPPED
        order.save(update_fields=["status"])

    ShipmentLine.objects.bulk_create(lines)
    return shipment


def orders_ready_to_despatch(warehouse):
    """Picked orders waiting at a warehouse, grouped by school — F42.

    What the despatch screen offers: a school, how many orders are ready for
    it, and how many units that is. Grouped because the van is per school.
    """
    from django.db.models import Count, Sum

    return (
        SchoolOrder.objects.filter(
            status=OrderStatus.PICKED, school__primary_warehouse=warehouse
        )
        .values("school_id", "school__name")
        .annotate(orders=Count("id", distinct=True), units=Sum("lines__quantity"))
        .order_by("school__name")
    )


def packing_list_for(shipment):
    """The document that travels with the goods — F40.

    AsOne's p.2 and p.8: it says what is in the parcel and which student it
    is for, so the school can hand the right uniform to the right child
    without opening anything.

    Two things it must carry, and both come from AsOne rather than from us:

    **The student's name and the invoice number together.** Their definitions
    page is explicit — "the Invoice# and Student's Name will be used by the
    school to deliver shipments to the correct students". Either alone is not
    enough: two children can share a name, and a number alone means nothing
    to the person handing out parcels.

    **Where it actually came from.** For a backorder filled elsewhere (D2)
    that is not the school's own warehouse, and a school receiving a parcel
    from Serere when it orders from Namayemba needs to see why.

    Not a PDF. This is the data; rendering is the frontend's job.
    """
    lines = shipment.lines.select_related("sku", "sku__garment", "order").all()

    return {
        "shipment_number": shipment.number,
        "shipped_on": shipment.shipped_on,
        "waybill_number": shipment.waybill_number,
        "from_warehouse": shipment.from_warehouse.name,
        "school": shipment.school.name,
        "school_address": shipment.school.address,
        "is_direct_from_another_warehouse": (
            shipment.from_warehouse_id != shipment.school.primary_warehouse_id
        ),
        # Every invoice number on the van. The school reconciles against
        # these, and after F42 there can be more than one.
        "order_numbers": [order.number for order in shipment.orders],
        # Flat, and every line names its student. AsOne's definitions page is
        # explicit that the school uses the invoice number *and* the
        # student's name together — either alone is not enough, because two
        # children can share a name and a number means nothing to the person
        # handing out parcels. On a consolidated van that pairing is the
        # whole document.
        "lines": [
            {
                "invoice_number": line.order.number,
                "student_name": line.order.student_name,
                "sku_number": line.sku.number,
                "description": line.sku.description,
                "quantity": line.quantity,
            }
            for line in lines
        ],
        "total_units": sum(line.quantity for line in lines),
    }


class CannotConfirmReceipt(Exception):
    """The shipment is not in a state where arrival can be confirmed."""


@transaction.atomic
def confirm_receipt(shipment, *, confirmed_by, notes=""):
    """The school says the parcel arrived — the other half of F41.

    ## Why this is a separate step from shipping

    Shipped means it left the warehouse. Completed means it got there. Those
    are different facts and the gap between them is the useful part: a
    parcel that left Namayemba three weeks ago and never arrived is
    invisible without it, and that gap is where losses live.

    **This does not touch the ledger.** Stock left at ship and stays gone.
    Moving it here would mean stock the warehouse has physically handed to a
    driver still counting as theirs for days — long enough for two
    warehouses to promise the same shirts.

    ## When the order completes

    Only once **every** shipment on it is confirmed. An order can have two:
    decision D2 lets a backorder go direct from a warehouse that is not the
    school's own. Completing on the first confirmation would close an order
    still waiting on a parcel.

    `notes` is for what was wrong — short, damaged, the wrong student. It is
    recorded and nothing acts on it: what to *do* about a bad delivery is a
    question AsOne has not answered, and inventing a process would be worse
    than leaving the note for a person to read.
    """
    if shipment.is_received:
        raise CannotConfirmReceipt(
            f"{shipment.number} was already confirmed on {shipment.received_at:%d %b %Y}."
        )
    live = [o for o in shipment.orders if o.status != OrderStatus.CANCELLED]
    if not live:
        raise CannotConfirmReceipt(
            f"Every order on {shipment.number} is cancelled."
        )

    shipment.received_at = timezone.now()
    shipment.received_by = confirmed_by
    shipment.receipt_notes = notes.strip()
    shipment.save(update_fields=["received_at", "received_by", "receipt_notes"])

    # F42: one van, several orders. Each is completed independently — an
    # order is done when every van carrying any part of it has been
    # confirmed, which for a split delivery is not the same day.
    for order in live:
        if order.shipments.filter(received_at__isnull=True).exists():
            continue
        order.status = OrderStatus.COMPLETED
        order.save(update_fields=["status"])

    return shipment


def shipments_awaiting_confirmation(warehouse=None, school=None, older_than_days=None):
    """What left the warehouse and nobody has confirmed arrived.

    The report the completion step exists to make possible. `older_than_days`
    narrows it to the ones actually worth chasing — everything shipped this
    morning is unconfirmed and none of it is a problem yet.
    """
    from datetime import timedelta

    shipments = (
        Shipment.objects.filter(received_at__isnull=True)
        .select_related("school", "from_warehouse")
        .prefetch_related("lines__order")
        # A van whose every order was cancelled is not worth chasing. One
        # that still carries a live order is, even if another on it was
        # cancelled — the goods are real either way.
        .exclude(lines__order__status=OrderStatus.CANCELLED)
        .distinct()
    )

    if warehouse is not None:
        shipments = shipments.filter(from_warehouse=warehouse)
    if school is not None:
        shipments = shipments.filter(school=school)
    if older_than_days is not None:
        cutoff = timezone.now().date() - timedelta(days=older_than_days)
        shipments = shipments.filter(shipped_on__lte=cutoff)

    return shipments.order_by("shipped_on")
