"""Master data — the tables Central Office owns.

Right now this is just the three site tables. The products side of the
catalog (Garment, Size, SKU, prices, Uniform Kits, reason codes) is the next
piece of work; when it lands, split this file into a `models/` package rather
than letting it grow past ~300 lines.

Vocabulary is AsOne's own, from p.2 of the 14 August pack. Worth keeping
straight, because three of the words sound similar but mean different things:

    Garment   a uniform component. "White Shirt". About 45 of them.
    Size      a garment attribute. Each garment comes in four or five.
    SKU       one garment in one size. What is counted, ordered and picked.
"""

from django.db import models


class TailoringCenter(models.Model):
    """Where uniforms are made. Idudi, Serere, Rwanyabihuka.

    Not system users — AsOne was explicit about this. Their packing lists are
    handwritten and the receiving warehouse keys them in. They exist as rows
    here so production orders and receipts have something to point at.
    """

    name = models.CharField(max_length=120, unique=True)
    address = models.TextField(blank=True)

    def __str__(self):
        return self.name


class Warehouse(models.Model):
    """Where finished stock is held. Namayemba and Serere.

    Namayemba also hosts AsOne's central office, which is why it is the
    proposed home for the database (open question Q12).
    """

    name = models.CharField(max_length=120, unique=True)
    address = models.TextField(blank=True)

    # "Warehouses have a primary TC but can order on any TC" (p.4), so this is
    # a default for production orders, not a restriction. Nullable because a
    # new warehouse may be set up before its Tailoring Center exists.
    primary_tailoring_center = models.ForeignKey(
        TailoringCenter, null=True, blank=True, on_delete=models.PROTECT
    )

    def __str__(self):
        return self.name


class School(models.Model):
    """An AsOne school. The customer, and the ship-to address.

    Students have no accounts. The school is the customer; the student's name
    is free text on the order, and the school hands the uniform over using the
    invoice number.
    """

    class Level(models.TextChoices):
        """Primary or High School.

        Drives which price list a school sees — AsOne asked for separate PS
        and HS price lists (p.7).
        """

        PRIMARY = "PS", "Primary School"
        HIGH = "HS", "High School"

    name = models.CharField(max_length=120, unique=True)
    level = models.CharField(max_length=2, choices=Level.choices)
    address = models.TextField(blank=True)

    # A school orders from this warehouse and no other — AsOne ruled out
    # cross-warehouse ordering. A backorder may still be *filled* by a
    # different warehouse shipping direct to the school; see
    # docs/CLIENT_DECISIONS.md D2, which matters when shipments are modelled.
    #
    # PROTECT, not CASCADE: deleting a warehouse must never silently delete
    # the schools that order from it.
    primary_warehouse = models.ForeignKey(
        Warehouse, on_delete=models.PROTECT, related_name="schools"
    )

    def __str__(self):
        return f"{self.name} ({self.get_level_display()})"
