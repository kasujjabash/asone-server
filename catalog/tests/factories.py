"""Shared fixtures for the catalog tests."""

from datetime import date
from decimal import Decimal

from catalog.models import Garment, GarmentPrice, Kit, KitItem

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


def make_kit(kit_number="PS-STARTER-01", name="PS Starter Kit", level=Kit.SchoolLevel.PRIMARY, **extra):
    return Kit.objects.create(kit_number=kit_number, name=name, school_level=level, **extra)


def make_kit_item(kit, sku, quantity=1):
    return KitItem.objects.create(kit=kit, sku=sku, quantity=quantity)
