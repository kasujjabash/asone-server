"""Shared fixtures for the catalog tests."""

from datetime import date
from decimal import Decimal

from catalog.models import Garment, GarmentPrice

# The 2027 season. Uniforms are made Sept–Dec 2026 and drawn down through 2027.
SEASON_START = date(2027, 1, 1)


def make_garment(name="White Shirt", level=Garment.SchoolLevel.BOTH, **extra):
    return Garment.objects.create(name=name, school_level=level, **extra)


def make_price(garment, amount="25000.00", active_from=SEASON_START, expires=None):
    """A price row. Amounts are strings so they never touch a float."""
    return GarmentPrice.objects.create(
        garment=garment,
        unit_price=Decimal(amount),
        active_date=active_from,
        expiration_date=expires,
    )
