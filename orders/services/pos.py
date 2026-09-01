"""Placing, invoicing and cancelling a school order — the point of sale.

Owned by the POS side. Fulfilment — availability, picking, shipping — lives
next door in fulfilment.py, which is warehouse-facing and a different role
entirely.

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

from ..models import SchoolOrder, SchoolOrderLine
from ..models.school_orders import OrderStatus

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


class CannotCancel(Exception):
    """The order has moved past the point where a school may withdraw it."""


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
    _refuse_empty_kits(kits)
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

    lines = _build_lines(order, kits, skus, order_date)
    if not lines:
        # Belt and braces. _refuse_empty_kits catches the known cause; this
        # catches any future one, because an order with no lines must never
        # reach a parent whatever produced it.
        raise EmptyOrder("That order would have nothing on it.")

    SchoolOrderLine.objects.bulk_create(lines)
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


def _refuse_empty_kits(kits):
    """A kit with no components cannot be ordered.

    Kits are created in the admin and filled in afterwards, so an empty one
    is a normal intermediate state — not a corrupt record. But ordering one
    produces an order with no lines and a total of nothing: a parent handed
    an invoice number for an empty parcel, and a warehouse with nothing to
    pick.

    Caught here rather than by checking the built lines, so the message says
    which kit is the problem instead of "your order came to nothing".
    """
    empty = [entry["kit"].kit_number for entry in kits if not entry["kit"].items.exists()]

    if empty:
        raise EmptyOrder(
            "These kits have no items in them yet, so there would be nothing "
            f"to supply: {', '.join(sorted(empty))}."
        )


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
# Cancelling — F36
# ---------------------------------------------------------------------------


@transaction.atomic
def cancel_order(order, *, cancelled_by, reason=""):
    """Withdraw an unpaid invoice — F36.

    The order is **cancelled, not deleted**. A school has handed that number
    to a parent; the document has to survive and say what became of it.

    Only while the order is still on Hold. F36 says "an unpaid invoice", and
    once payment is confirmed and the order released, cancelling raises
    questions nobody has answered — whether picked stock goes back on the
    shelf, and what happens to money already taken. That is open question Q5,
    so this refuses rather than inventing an answer.

    Cancelling twice is refused too. It would overwrite who cancelled it and
    when, which is the only record of the decision.
    """
    if not order.can_be_cancelled:
        raise CannotCancel(
            f"{order.number} is {order.get_status_display().lower()} and can no "
            "longer be cancelled by the school. Only an unpaid order on hold can be."
        )

    order.status = OrderStatus.CANCELLED
    order.cancelled_at = timezone.now()
    order.cancelled_by = cancelled_by
    order.cancellation_reason = reason.strip()
    order.save(
        update_fields=["status", "cancelled_at", "cancelled_by", "cancellation_reason"]
    )
    return order


# ---------------------------------------------------------------------------
# The invoice — F34
# ---------------------------------------------------------------------------


def invoice_for(order):
    """The order as a document a school can hand to a parent.

    AsOne's definition (p.2): "created by the system with Uniform Kit # (if
    used), SKUs and quantities costed for payment". So kit lines are grouped
    back under the kit the school actually chose, rather than presented as a
    flat list of garments nobody asked for by name.

    That regrouping is presentation only. The order's lines stay as they are
    — individual SKUs, which is what the warehouse picks.
    """
    kits, loose = {}, []

    for line in order.lines.select_related("sku", "sku__garment", "from_kit"):
        if line.from_kit is None:
            loose.append(line)
            continue

        group = kits.setdefault(
            line.from_kit_id,
            {"kit": line.from_kit, "lines": [], "subtotal": 0},
        )
        group["lines"].append(line)
        group["subtotal"] += line.line_total

    return {
        "kits": sorted(kits.values(), key=lambda g: g["kit"].kit_number),
        "items": sorted(loose, key=lambda line: line.sku.description),
    }


# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------


def orders_on_hold(queryset=None):
    """Invoices raised but not yet paid or released — F53.

    The queue a school works from: what is waiting on a parent to pay. Also
    what the leads look at to see whether the point of sale is being used.

    Takes a queryset so the caller can scope it first — a school sees its
    own, the leads see every school.
    """
    orders = SchoolOrder.objects.all() if queryset is None else queryset

    return (
        orders.filter(status=OrderStatus.HOLD)
        .select_related("school")
        .prefetch_related("lines")
        .order_by("order_date", "number")
    )
