"""Shared fixtures for the accounts tests.

One place that knows how to build a valid user of each role, so a change to
the role/site invariant does not have to be chased through every test file.
"""

from django.contrib.auth import get_user_model

from catalog.models import School, TailoringCenter, Warehouse

User = get_user_model()

PASSWORD = "correct-horse-battery-staple"


def build_sites():
    """AsOne's real sites, trimmed to what the tests need.

    Two warehouses and two schools, because most of these tests are about one
    site not being able to see the other.
    """
    idudi = TailoringCenter.objects.create(name="Idudi")
    namayemba = Warehouse.objects.create(name="Namayemba", primary_tailoring_center=idudi)
    serere = Warehouse.objects.create(name="Serere")

    return {
        "namayemba": namayemba,
        "serere": serere,
        "school_a": School.objects.create(
            name="Namayemba PS", level=School.Level.PRIMARY, primary_warehouse=namayemba
        ),
        "school_b": School.objects.create(
            name="Serere HS", level=School.Level.HIGH, primary_warehouse=serere
        ),
    }


def make_user(email, role, *, warehouse=None, school=None, **extra):
    """Create an active user. Sites are passed only where the role allows one.

    `email` may be given as a bare name — "julius" becomes
    "julius@asone.test" — to keep the tests readable.
    """
    if "@" not in email:
        email = f"{email}@asone.test"

    return User.objects.create_user(
        email=email,
        password=PASSWORD,
        role=role,
        warehouse=warehouse,
        school=school,
        **extra,
    )
