"""Costed reports for Finance — F55 and F56.

Read-only aggregations over documents that already exist. Nothing here
records anything new; it adds up what the orders and receipts already say.

Two rules run through both:

**Value comes from the document, not from today's price list.** A group order
placed in September is worth what was agreed in September, whatever a garment
costs by the time the report is run. The order line already carries that
figure, snapshotted when the order was raised.

**Only posted receipts count.** An unposted receipt is paperwork somebody is
still checking against the Tailoring Center's handwritten packing list — not
goods received, and not money owed.
"""

from django.db.models import Count, DecimalField, F, IntegerField, Sum, Value
from django.db.models.functions import Coalesce

from .models import GroupOrder, Receipt, ReceiptLine
from .models.base import OrderStatus

#: Money always sums as a decimal. quantity is an integer and unit_price a
#: decimal, and Django will not guess what their product should be — an
#: explicit output_field is required, and for money the answer is obvious.
MONEY = DecimalField(max_digits=18, decimal_places=2)
ZERO = Value(0, output_field=MONEY)


def _within(queryset, field, date_from=None, date_to=None):
    """Narrow to a period. Both ends are inclusive, as a person would expect
    when they ask for "September"."""
    if date_from:
        queryset = queryset.filter(**{f"{field}__gte": date_from})
    if date_to:
        queryset = queryset.filter(**{f"{field}__lte": date_to})
    return queryset


# ---------------------------------------------------------------------------
# F55 — Group orders, costed
# ---------------------------------------------------------------------------


def group_orders_costed(date_from=None, date_to=None, include_cancelled=False):
    """What was committed to the Tailoring Centers, per group order.

    AsOne uses the group order to fund the TCs, so this is the report behind
    that funding: what was ordered, and what it was worth on the day.

    Cancelled orders are excluded by default — money was never committed
    against a withdrawn order, and including it would overstate the funding.
    Pass ``include_cancelled=True`` to see them for reconciliation.

    One query.
    """
    orders = _within(GroupOrder.objects.all(), "order_date", date_from, date_to)
    if not include_cancelled:
        orders = orders.exclude(status=OrderStatus.CANCELLED)

    return orders.annotate(
        line_count=Count("lines", distinct=True),
        quantity=Coalesce(Sum("lines__quantity"), Value(0), output_field=IntegerField()),
        value=Coalesce(
            Sum(F("lines__quantity") * F("lines__unit_price"), output_field=MONEY),
            ZERO,
            output_field=MONEY,
        ),
    ).order_by("-order_date", "-number")


def group_order_total(date_from=None, date_to=None, include_cancelled=False):
    """The single figure Finance usually wants: everything committed."""
    rows = group_orders_costed(date_from, date_to, include_cancelled)
    return {
        "orders": rows.count(),
        "quantity": sum(row.quantity for row in rows),
        "value": sum(row.value for row in rows),
    }


# ---------------------------------------------------------------------------
# F56 — Receipts from Tailoring Centers, costed
# ---------------------------------------------------------------------------


def receipts_costed(date_from=None, date_to=None, tailoring_center=None, warehouse=None):
    """What each Tailoring Center actually delivered, and what it was worth.

    Valued at what AsOne agreed to pay that TC, which each receipt line now
    carries — copied from the production order line when the receipt was
    entered. A short delivery is worth what arrived, not what the packing
    list claimed.

    Grouped by Tailoring Center, because that is the question Finance asks:
    how much do we owe Idudi for this season.

    Only posted receipts. An unposted one is paperwork somebody is still
    checking, not goods received and not money owed.

    One query.
    """
    lines = ReceiptLine.objects.filter(receipt__posted_at__isnull=False)
    lines = _within(lines, "receipt__date_received", date_from, date_to)

    if tailoring_center is not None:
        lines = lines.filter(receipt__production_order__tailoring_center=tailoring_center)
    if warehouse is not None:
        lines = lines.filter(receipt__production_order__warehouse=warehouse)

    return (
        lines.values(
            tailoring_center_id=F("receipt__production_order__tailoring_center"),
            tailoring_center_name=F("receipt__production_order__tailoring_center__name"),
        )
        .annotate(
            receipts=Count("receipt", distinct=True),
            quantity=Coalesce(
                Sum("quantity_received"), Value(0), output_field=IntegerField()
            ),
            value=Coalesce(
                Sum(F("quantity_received") * F("unit_value"), output_field=MONEY),
                ZERO,
                output_field=MONEY,
            ),
        )
        .order_by("tailoring_center_name")
    )


def receipt_detail_costed(date_from=None, date_to=None, tailoring_center=None):
    """The same thing receipt by receipt, for checking a total.

    A summary nobody can drill into is a number people stop trusting.
    """
    receipts = Receipt.objects.filter(posted_at__isnull=False).select_related(
        "production_order__tailoring_center", "production_order__warehouse"
    )
    receipts = _within(receipts, "date_received", date_from, date_to)
    if tailoring_center is not None:
        receipts = receipts.filter(production_order__tailoring_center=tailoring_center)

    return receipts.annotate(
        quantity=Coalesce(
            Sum("lines__quantity_received"), Value(0), output_field=IntegerField()
        ),
        value=Coalesce(
            Sum(
                F("lines__quantity_received") * F("lines__unit_value"),
                output_field=MONEY,
            ),
            ZERO,
            output_field=MONEY,
        ),
    ).order_by("-date_received", "-number")
