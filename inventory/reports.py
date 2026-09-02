"""Costed inventory adjustments — F58.

What the adjustments did to the value of stock, grouped by reason code: how
many units, and what they were worth at the value the ledger already carries
them at.

## What is built, and what is deliberately not

The **operational** half is fully computable today and is what this file
does. Every `StockMovement` already carries a `unit_value`, so "we wrote off
1,240,000 shillings of damaged stock at Namayemba in October" needs no new
data and no decision from anybody.

The **accounting** half is open question Q6. AsOne's p.6 gives each kind of
adjustment a financial note — returns credit the student, transfers move
nothing — and marks Damages "To be determined". So this report says what the
value *was*; it does not say which ledger account it should land in, whether
a write-off is an expense or a loss, or who absorbs it.

`FINANCIAL_TREATMENT` below is the seam. It is empty on purpose. When AsOne
answers Q6, fill it in there — not by scattering the rule through this file,
and not by putting behaviour on `ReasonCode`, which deliberately carries
none for exactly this reason (see its docstring).
"""

from decimal import Decimal

from django.db.models import DecimalField, F, IntegerField, Sum, Value
from django.db.models.functions import Coalesce

from .models import InventoryAdjustment, MovementType, StockMovement

MONEY = DecimalField(max_digits=18, decimal_places=2)

#: How each reason code is treated financially — **open question Q6.**
#:
#: Keyed by reason code, e.g. ``{"DMG": "EXPENSE", "RET": "CREDIT_STUDENT"}``.
#: Empty until AsOne answers, and the report reports "not yet classified"
#: rather than guessing. Their own p.6 marks Damages "To be determined", so
#: an empty mapping is the honest state, not an oversight.
#:
#: When it is answered: fill this in, and the `treatment` column starts
#: carrying it. Nothing else in this file changes.
FINANCIAL_TREATMENT: dict[str, str] = {}

#: What the report says while Q6 is unanswered.
UNCLASSIFIED = "Not yet classified — AsOne question Q6"

#: Bucket for an ADJUSTMENT movement with no adjustment document behind it.
UNKNOWN_CODE = "UNKNOWN"


def _within(queryset, field, date_from=None, date_to=None):
    """Narrow to a period. Both ends inclusive, as a person expects when
    they ask for "October"."""
    if date_from:
        queryset = queryset.filter(**{f"{field}__gte": date_from})
    if date_to:
        queryset = queryset.filter(**{f"{field}__lte": date_to})
    return queryset


def adjustments_costed(date_from=None, date_to=None, warehouse=None):
    """Adjustments by reason code, counted and valued — F58.

    One row per reason code: how many adjustments, how many units, and what
    those units were worth at the value the ledger carries them at.

    **Value is signed the way the ledger is signed.** A damage of five
    shirts is a negative value because stock left; a return is positive
    because stock came back. That means the rows sum to the net effect on
    the value of stock, which is the number Finance actually wants.

    Read from the movements rather than from the adjustment documents, for
    the same reason every stock figure in this system is: the ledger is what
    actually happened. An adjustment that was created and never posted has
    moved nothing and is correctly absent here.

    One query.
    """
    movements = StockMovement.objects.filter(movement_type=MovementType.ADJUSTMENT)
    movements = _within(movements, "occurred_on", date_from, date_to)
    if warehouse is not None:
        movements = movements.filter(warehouse=warehouse)

    # The reason code lives on the adjustment document, not on the movement,
    # and they are joined by document number rather than a foreign key. So
    # the grouping happens here: one row per adjustment out of the database,
    # folded into one row per code. That is a handful of rows per period,
    # not a scan — the aggregation itself is still one query.
    adjustments = {
        number: (code, name)
        for number, code, name in InventoryAdjustment.objects.values_list(
            "number", "reason_code__code", "reason_code__name"
        )
    }

    per_document = movements.values("document_number").annotate(
        units=Coalesce(Sum("quantity"), Value(0), output_field=IntegerField()),
        value=Coalesce(
            Sum(F("quantity") * F("unit_value"), output_field=MONEY),
            Value(Decimal("0.00"), output_field=MONEY),
            output_field=MONEY,
        ),
    )

    totals: dict[str, dict] = {}
    for row in per_document:
        # A movement typed ADJUSTMENT with no adjustment behind it should not
        # exist. Bucketed separately rather than folded into a real code, so
        # that if it ever happens somebody sees it instead of a wrong total.
        code, name = adjustments.get(row["document_number"], (UNKNOWN_CODE, ""))

        bucket = totals.setdefault(
            code,
            {
                "reason_code": code,
                "reason_name": name,
                "adjustments": 0,
                "units": 0,
                "value": Decimal("0.00"),
            },
        )
        bucket["adjustments"] += 1
        bucket["units"] += row["units"]
        bucket["value"] += row["value"]

    for bucket in totals.values():
        bucket["treatment"] = FINANCIAL_TREATMENT.get(
            bucket["reason_code"], UNCLASSIFIED
        )

    return sorted(totals.values(), key=lambda row: row["reason_code"])
