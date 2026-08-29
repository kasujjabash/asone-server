"""Business logic for ordering from the Tailoring Centers.

Thin views, fat services. Everything here is callable from the API, the
admin, a management command or a test without going near HTTP.

Two rules run through all of it:

    An order line's price is fixed when the order is placed.
    Production orders should sum back up to their group order.

The first is enforced. The second is *reported*, not enforced — AsOne says
production orders "should initially sum up" to the group order, and a
warehouse that legitimately orders a little extra should not be blocked by
the system. `reconcile()` shows the difference and lets a person judge it.
"""

from collections import defaultdict
from datetime import date

from django.db import connection, transaction

from catalog.services import price_for

from django.utils import timezone

from inventory.models import MovementType, StockStatus
from inventory.services import post_movement

from .models import (
    GroupOrder,
    GroupOrderLine,
    ProductionOrder,
    ProductionOrderLine,
    Receipt,
    ReceiptLine,
)
from .models.base import OrderStatus

#: Created by the initial migration. Numbers are prefixed so a document is
#: identifiable on sight — a warehouse clerk reading a handwritten note can
#: tell a group order from a production order without looking it up.
GROUP_ORDER_SEQUENCE = "procurement_group_order_seq"
PRODUCTION_ORDER_SEQUENCE = "procurement_production_order_seq"
RECEIPT_SEQUENCE = "procurement_receipt_seq"


class OrderHasNoLines(Exception):
    """An order with no lines is not an order.

    Raised rather than quietly saving an empty document, which would sit in
    the open-orders view forever waiting for goods nobody asked for.
    """


def _next_number(sequence: str, prefix: str) -> str:
    """Draw the next document number.

    A Postgres sequence, for the same reasons as SKU numbers: `nextval` is
    atomic, so two people raising orders at the same instant cannot collide,
    and a sequence never goes backwards, so a cancelled order does not free
    its number for reuse. Gaps are harmless; collisions are not.
    """
    with connection.cursor() as cursor:
        cursor.execute("SELECT nextval(%s)", [sequence])
        return f"{prefix}{cursor.fetchone()[0]}"


def next_group_order_number() -> str:
    return _next_number(GROUP_ORDER_SEQUENCE, "GO-")


def next_production_order_number() -> str:
    return _next_number(PRODUCTION_ORDER_SEQUENCE, "PO-")


def next_receipt_number() -> str:
    return _next_number(RECEIPT_SEQUENCE, "RC-")


# ---------------------------------------------------------------------------
# Raising an order
# ---------------------------------------------------------------------------


def line_price(sku, on_date):
    """The price to write onto a line, taken from the SKU's garment.

    Lets `PriceNotSet` propagate. An order for an unpriced garment cannot be
    costed, and AsOne uses the group order to fund the Tailoring Centers — a
    line silently worth nothing would under-fund them.
    """
    return price_for(sku.garment, on_date)


@transaction.atomic
def create_group_order(*, created_by, lines, order_date=None, **fields):
    """Raise a group order with its lines in one transaction.

    ``lines`` is an iterable of ``{"sku": Sku, "quantity": int}`` and
    optionally ``"unit_price"``. Where no price is given, the garment's price
    on ``order_date`` is copied onto the line and fixed there.

    Atomic because a half-written order — a header with no lines — would show
    up in every open-orders view as something waiting to arrive.
    """
    order_date = order_date or date.today()
    lines = list(lines)
    if not lines:
        raise OrderHasNoLines("A group order needs at least one line.")

    order = GroupOrder.objects.create(
        created_by=created_by, order_date=order_date, **fields
    )
    _write_lines(GroupOrderLine, order, lines, order_date)
    return order


@transaction.atomic
def create_production_order(*, created_by, lines, order_date=None, **fields):
    """Raise a production order on a Tailoring Center. See create_group_order."""
    order_date = order_date or date.today()
    lines = list(lines)
    if not lines:
        raise OrderHasNoLines("A production order needs at least one line.")

    order = ProductionOrder.objects.create(
        created_by=created_by, order_date=order_date, **fields
    )
    _write_lines(ProductionOrderLine, order, lines, order_date)
    return order


def _write_lines(line_model, order, lines, order_date):
    """Snapshot the price onto each line and save them in one round trip."""
    line_model.objects.bulk_create(
        [
            line_model(
                order=order,
                sku=line["sku"],
                quantity=line["quantity"],
                unit_price=line.get("unit_price") or line_price(line["sku"], order_date),
            )
            for line in lines
        ]
    )


# ---------------------------------------------------------------------------
# Reconciliation
# ---------------------------------------------------------------------------


def reconcile(group_order):
    """Compare a group order against the production orders placed under it.

    AsOne's rule is that production orders "should initially sum up to the
    Group Order". Reported rather than enforced: a warehouse ordering a
    little extra is a judgement call for a person, not an error for the
    system, and blocking it would mean blocking safety stock.

    Returns one row per SKU that appears on either side::

        {"sku": Sku, "ordered": 500, "requested": 480, "difference": -20}

    `difference` is production minus group: negative means the TCs have been
    asked for less than the requirement, which is the case worth chasing.
    """
    requested = defaultdict(int)
    for line in group_order.lines.select_related("sku", "sku__garment"):
        requested[line.sku] += line.quantity

    ordered = defaultdict(int)
    # Cancelled orders are excluded: they were withdrawn, so counting them
    # would show the requirement as covered by goods nobody is making.
    for line in (
        ProductionOrderLine.objects.filter(order__group_order=group_order)
        .exclude(order__status=OrderStatus.CANCELLED)
        .select_related("sku", "sku__garment")
    ):
        ordered[line.sku] += line.quantity

    return [
        {
            "sku": sku,
            "requested": requested.get(sku, 0),
            "ordered": ordered.get(sku, 0),
            "difference": ordered.get(sku, 0) - requested.get(sku, 0),
        }
        for sku in sorted(set(requested) | set(ordered), key=lambda s: s.description)
    ]


def open_production_orders(queryset=None):
    """Orders placed on the TCs but not yet closed — F22.

    Receipts are not built yet, so "open" currently means status OPEN. Once
    receipts exist this should also account for orders received in full but
    not yet closed by hand.
    """
    queryset = queryset if queryset is not None else ProductionOrder.objects.all()
    return queryset.filter(status=OrderStatus.OPEN)


# ---------------------------------------------------------------------------
# Receipts — F19, F20, F21
# ---------------------------------------------------------------------------


class ReceiptAlreadyPosted(Exception):
    """Posting twice would double the stock.

    A receipt is posted once. If the count was wrong, the correction is an
    inventory adjustment against the ledger, not a second posting — the
    ledger is append-only, so there is no way to "re-post" over the first.
    """


class NotOnTheOrder(Exception):
    """A receipt line names a SKU the production order never asked for.

    Refused rather than accepted quietly. A TC shipping something nobody
    ordered is a real event, but it needs a person to decide what to do — it
    is not something a warehouse clerk should be able to absorb into stock by
    keying it in.
    """

    def __init__(self, skus):
        self.skus = skus
        listed = ", ".join(sorted(sku.number for sku in skus))
        super().__init__(
            f"These SKUs are not on the production order: {listed}."
        )


@transaction.atomic
def create_receipt(*, production_order, lines, created_by, **fields):
    """Record what arrived, without touching stock.

    Entering and posting are deliberately separate steps. AsOne's flow has
    the warehouse *check the delivery against the TC's handwritten packing
    list and resolve differences* before anything is committed — so a receipt
    can be keyed in, compared, corrected, and only then posted.

    ``lines`` is an iterable of dicts with ``sku`` and ``quantity_received``,
    optionally ``quantity_on_packing_list`` and ``discrepancy_note``.
    """
    lines = list(lines)
    if not lines:
        raise OrderHasNoLines("A receipt needs at least one line.")

    # The order line price is what AsOne agreed to pay this TC. Snapshotted
    # onto each receipt line so the document records what the goods are
    # worth, and the ledger and Finance's report both read the same figure.
    order_prices = {
        line.sku_id: line.unit_price for line in production_order.lines.all()
    }
    unexpected = [line["sku"] for line in lines if line["sku"].pk not in order_prices]
    if unexpected:
        raise NotOnTheOrder(unexpected)

    receipt = Receipt(production_order=production_order, created_by=created_by, **fields)
    receipt.full_clean(exclude=["number", "created_by", "production_order"])
    receipt.save()

    ReceiptLine.objects.bulk_create(
        [
            ReceiptLine(
                receipt=receipt,
                sku=line["sku"],
                quantity_received=line["quantity_received"],
                quantity_on_packing_list=line.get("quantity_on_packing_list"),
                discrepancy_note=line.get("discrepancy_note", ""),
                unit_value=order_prices[line["sku"].pk],
            )
            for line in lines
        ]
    )
    return receipt


@transaction.atomic
def post_receipt(receipt, *, posted_by):
    """Commit a receipt to the ledger — F21.

    Writes one permanent movement per line and stamps the receipt as posted.
    Atomic: a receipt that is half in the ledger would overstate some SKUs
    and understate others, and no later count could tell which.

    Each movement is valued at **the price on the production order line**,
    not today's price list. AsOne buys from the TCs at an agreed figure; the
    stock is worth what was paid for it.

    Returns the movements written.
    """
    if receipt.is_posted:
        raise ReceiptAlreadyPosted(
            f"{receipt.number} was already posted on {receipt.posted_at:%Y-%m-%d}."
        )

    order = receipt.production_order

    movements = [
        post_movement(
            warehouse=order.warehouse,
            sku=line.sku,
            quantity=line.quantity_received,
            movement_type=MovementType.RECEIPT,
            stock_status=StockStatus.AVAILABLE,
            unit_value=line.unit_value,
            document_number=receipt.number,
            occurred_on=receipt.date_received,
            created_by=posted_by,
            source=order.tailoring_center.name,
            destination=order.warehouse.name,
        )
        for line in receipt.lines.select_related("sku")
    ]

    receipt.posted_at = timezone.now()
    receipt.save(update_fields=["posted_at"])

    return movements


def outstanding_on_order(production_order):
    """What is still to come on an order: ordered minus received.

    Counts posted receipts only. An unposted receipt is paperwork someone is
    still checking, not goods the warehouse can rely on.
    """
    received = defaultdict(int)
    for line in ReceiptLine.objects.filter(
        receipt__production_order=production_order,
        receipt__posted_at__isnull=False,
    ).select_related("sku"):
        received[line.sku_id] += line.quantity_received

    return [
        {
            "sku": line.sku,
            "ordered": line.quantity,
            "received": received.get(line.sku_id, 0),
            "outstanding": line.quantity - received.get(line.sku_id, 0),
        }
        for line in production_order.lines.select_related("sku")
    ]
