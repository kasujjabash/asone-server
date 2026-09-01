"""Business logic for school orders.

Placing an order is the one thing here worth reading closely. Everything
else follows from two rules AsOne set out on p.7:

    A school orders at kit or item level.
    The warehouse always picks individual SKUs.

So a kit does not survive as a kit. It is exploded into its components when
the order is placed, and the components are what gets stored.
"""

from collections import defaultdict

from django.db import connection, transaction
from django.utils import timezone

from catalog.services import price_for_sku

from .models import SchoolOrder, SchoolOrderLine
from .models.school_orders import OrderStatus

ORDER_SEQUENCE = "orders_school_order_seq"


def next_order_number() -> str:
    """The next order-and-invoice number.

    A Postgres sequence, for the same two reasons as everywhere else: atomic
    under concurrency, and never reused. It matters more here than most — the
    school hands this number to a parent, and it has to mean one order
    forever.
    """
    with connection.cursor() as cursor:
        cursor.execute("SELECT nextval(%s)", [ORDER_SEQUENCE])
        return f"SO-{cursor.fetchone()[0]}"


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class EmptyOrder(Exception):
    """An order with nothing on it. Refused rather than saved as a shell."""


class InactiveItem(Exception):
    """Something on the order has been retired and cannot be sold.

    Checked at the point of sale rather than discovered in the warehouse,
    where the answer is a pick list nobody can complete.
    """

    def __init__(self, labels):
        self.labels = labels
        listed = ", ".join(sorted(labels))
        super().__init__(f"These are no longer available to order: {listed}.")


class WrongSchoolLevel(Exception):
    """A Primary school ordering a High School kit, or the reverse.

    The same rule that decides which price list a school works from (F29):
    a school orders from its own level, and from garments marked for both.
    """

    def __init__(self, labels, school):
        self.labels = labels
        listed = ", ".join(sorted(labels))
        super().__init__(
            f"{school.name} is a {school.get_level_display()} and cannot order: {listed}."
        )


# ---------------------------------------------------------------------------
# Placing an order
# ---------------------------------------------------------------------------


@transaction.atomic
def place_order(*, school, student_name, order_date, kits=(), skus=(), created_by, **fields):
    """Create a school order on Hold — F30, F31, F32, F33.

    ``kits`` and ``skus`` are both iterables of ``{"kit"/"sku", "quantity"}``.
    A school may send either or both; an order needs at least one line
    between them.

    Every kit is exploded into its component SKUs here (F33), and those
    components are **stored**. If Central Office edits the kit's bill of
    materials next term, this order still means what it meant today. Same
    reasoning as a dated price: a document records what was true when it was
    written.

    The order lands on Hold (F32) and stays there. Releasing it depends on
    payment being confirmed, and what confirms payment is open question Q2 —
    so nothing here can move it on.

    Raises EmptyOrder, InactiveItem, WrongSchoolLevel, or PriceNotSet if
    anything ordered has no price on the day.
    """
    kits, skus = list(kits), list(skus)
    if not kits and not skus:
        raise EmptyOrder("An order needs at least one kit or item.")

    _refuse_retired(kits, skus)
    _refuse_wrong_level(kits, skus, school)

    order = SchoolOrder(
        school=school,
        student_name=student_name,
        order_date=order_date,
        status=OrderStatus.HOLD,
        created_by=created_by,
        **fields,
    )
    order.full_clean(exclude=["number", "created_by", "school"])
    order.save()

    SchoolOrderLine.objects.bulk_create(_build_lines(order, kits, skus, order_date))
    return order


def _build_lines(order, kits, skus, on_date):
    """Turn what the school asked for into the SKU lines a warehouse picks.

    Kit lines and individually-ordered lines are kept apart rather than
    merged, even when they name the same SKU. Two shirts inside a kit and one
    ordered loose is three shirts — but the invoice has to be able to show the
    school which two came from the kit it chose.
    """
    lines = []

    for entry in kits:
        kit, kit_quantity = entry["kit"], entry["quantity"]
        for item in kit.items.select_related("sku", "sku__garment"):
            lines.append(
                SchoolOrderLine(
                    order=order,
                    sku=item.sku,
                    quantity=item.quantity * kit_quantity,
                    unit_price=price_for_sku(item.sku, on_date),
                    from_kit=kit,
                )
            )

    # Two of the same kit on one order is one set of lines at double quantity,
    # not two sets — the unique constraint is per (order, sku, kit), and the
    # school asked for the same thing twice.
    lines = _merge_by_source(lines)

    for entry in skus:
        sku = entry["sku"]
        lines.append(
            SchoolOrderLine(
                order=order,
                sku=sku,
                quantity=entry["quantity"],
                unit_price=price_for_sku(sku, on_date),
                from_kit=None,
            )
        )

    return lines


def _merge_by_source(lines):
    """Combine lines that share both a SKU and the kit they came from."""
    merged = {}
    for line in lines:
        key = (line.sku_id, line.from_kit_id)
        if key in merged:
            merged[key].quantity += line.quantity
        else:
            merged[key] = line
    return list(merged.values())


def _refuse_retired(kits, skus):
    """Nothing retired may be sold.

    A kit is checked as a whole and component by component: an active kit
    containing a retired shirt is just as unfillable as a retired kit.
    """
    retired = []

    for entry in kits:
        kit = entry["kit"]
        if not kit.is_active:
            retired.append(f"kit {kit.kit_number}")
        retired.extend(
            item.sku.number
            for item in kit.items.select_related("sku")
            if not item.sku.is_active
        )

    retired.extend(entry["sku"].number for entry in skus if not entry["sku"].is_active)

    if retired:
        raise InactiveItem(retired)


def _refuse_wrong_level(kits, skus, school):
    """A school orders from its own level, and from anything marked for both.

    The same rule as the price list (F29). Without it, a Primary school could
    order a High School blazer that never appears on its own price list.
    """
    wrong = [
        f"kit {entry['kit'].kit_number}"
        for entry in kits
        if entry["kit"].school_level != school.level
    ]

    wrong.extend(
        entry["sku"].number
        for entry in skus
        if not entry["sku"].garment.appears_on_price_list(school.level)
    )

    if wrong:
        raise WrongSchoolLevel(wrong, school)


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------


def order_demand(order):
    """What the warehouse actually has to pick, per SKU.

    Lines are kept split by source so an invoice can show the school what it
    chose; a pick list does not care. This is the other view: one row per
    SKU, however many places it came from.
    """
    demand = defaultdict(int)
    for line in order.lines.select_related("sku"):
        demand[line.sku] += line.quantity

    return sorted(demand.items(), key=lambda pair: pair[0].description)


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

    Refused if the order is cancelled or already picked/shipped. Deliberately
    **not** gated on OrderStatus.RELEASED: nothing in this codebase can reach
    that status yet — releasing depends on payment confirmation, open
    question Q2 — so requiring it here would make F39 permanently
    unreachable. Revisit once Q2 is settled.

    Refused, atomically, if any line is short — see OrderNotFillable and
    check_availability().
    """
    if order.status == OrderStatus.CANCELLED:
        raise OrderCannotBePicked(f"{order.number} is cancelled and cannot be picked.")
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
