"""Time-effective garment prices.

AsOne's pricing table (p.3) carries a part number, a unit price, an active
date and an expiration date. Two consequences shape this module:

**Price is dated, not current.** A price is not a number on a product; it is a
number that applied over a period. An invoice raised in March must still cost
out at March's price when it is reprinted in September, so nothing here ever
overwrites a price — a new one is added with a later active date.

**Price hangs off Garment, not SKU.** "All White Shirts have the same price
regardless of size" (p.2). See catalog/models/products.py.

Purchase price equals selling price throughout — AsOne buys from the Tailoring
Centers and sells to students at the same figure, so there is one price column
rather than a cost and a margin.
"""

from decimal import Decimal

from django.contrib.postgres.constraints import ExclusionConstraint
from django.contrib.postgres.fields import DateRangeField, RangeBoundary, RangeOperators
from django.core.validators import MinValueValidator
from django.db import models
from django.db.models import Func, Q


class DateRange(Func):
    """`daterange(active_date, expiration_date, '[)')` in SQL.

    Used only by the exclusion constraint below. The `[)` boundary makes the
    start inclusive and the end exclusive, so consecutive prices chain without
    a gap and without overlapping: one ending 1 June and the next starting
    1 June meet exactly.
    """

    function = "daterange"
    output_field = DateRangeField()


class GarmentPrice(models.Model):
    """What a garment sold for, over a period.

    Rows are never edited to change a price. To reprice, close the current row
    by setting its expiration date and add a new one — history is what makes a
    reprinted invoice match the original.
    """

    garment = models.ForeignKey(
        "catalog.Garment", on_delete=models.PROTECT, related_name="prices"
    )

    # Decimal, never float. Binary floating point cannot represent 0.1
    # exactly, and a rounding error in a kit price is a rounding error on an
    # invoice. 12 digits carries prices well past anything in Ugandan
    # shillings.
    unit_price = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        validators=[MinValueValidator(Decimal("0.01"))],
        help_text="Purchase price and selling price — AsOne uses one figure for both.",
    )

    active_date = models.DateField(help_text="First day this price applies.")

    # Exclusive, and named to say so in the admin. Inclusive end dates make
    # consecutive prices ambiguous on the changeover day, which is exactly the
    # day someone raises an invoice and asks which price was right.
    expiration_date = models.DateField(
        null=True,
        blank=True,
        help_text="First day this price NO LONGER applies. Leave blank for open-ended.",
    )

    class Meta:
        ordering = ["garment", "-active_date"]
        get_latest_by = "active_date"
        constraints = [
            models.CheckConstraint(
                condition=Q(unit_price__gt=0),
                name="garment_price_is_positive",
            ),
            models.CheckConstraint(
                condition=Q(expiration_date__isnull=True)
                | Q(expiration_date__gt=models.F("active_date")),
                name="garment_price_expires_after_it_starts",
            ),
            # The important one. Without it, two rows can claim the same
            # garment on the same day and the price of a shirt becomes a
            # question rather than a fact. Enforced by Postgres, so it holds
            # against the admin, a management command, a data import and two
            # simultaneous requests alike.
            #
            # Needs the btree_gist extension to combine an equality test on
            # garment_id with an overlap test on the date range — see the
            # migration.
            ExclusionConstraint(
                name="no_overlapping_garment_prices",
                expressions=[
                    (
                        DateRange("active_date", "expiration_date", RangeBoundary()),
                        RangeOperators.OVERLAPS,
                    ),
                    ("garment", RangeOperators.EQUAL),
                ],
                violation_error_message=(
                    "This garment already has a price covering part of that period. "
                    "Close the existing price first."
                ),
            ),
        ]

    def __str__(self):
        return f"{self.garment} @ {self.unit_price} from {self.active_date}"

    def applies_on(self, on_date) -> bool:
        """Whether this price was in force on ``on_date``.

        Mirrors the `[)` boundary of the exclusion constraint: the active date
        counts, the expiration date does not.
        """
        if on_date < self.active_date:
            return False
        return self.expiration_date is None or on_date < self.expiration_date
