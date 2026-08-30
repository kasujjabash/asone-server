"""Split the inventory correction code in two — one up, one down.

F23 gave every reason code a fixed direction, which is right for the codes
AsOne named: a return always adds, damage always removes. A physical count
correction is the exception — the count can come in above the system figure
as easily as below it — so a single CORR code fixed to DECREASE would let
F24 post shortfalls and nothing else.

The alternative was to let an adjustment override its code's direction. That
was rejected: it makes one code behave unlike all the others, and "the reason
code decides the direction" stops being true everywhere. Two codes keeps the
rule intact, and Finance picks from a list either way.

CORR is **retired, not deleted**. Adjustments may already point at it, and an
audit trail that cannot say why a movement happened is not an audit trail.
"""

from django.db import migrations

REPLACEMENTS = [
    (
        "CORR_UP",
        "Inventory correction — count higher",
        "A physical count found more than the system expected.",
        "INCREASE",
    ),
    (
        "CORR_DOWN",
        "Inventory correction — count lower",
        "A physical count found less than the system expected.",
        "DECREASE",
    ),
]


def split_correction(apps, schema_editor):
    """Split CORR in two — but only where there is a CORR to split.

    Guarded deliberately. A migration transforms data that is already there;
    it is not where master data comes from. A fresh database has no reason
    codes at all until `seed_demo` runs, and having this quietly create two
    of them would mean every test database carried master data nobody put
    there — implicit fixtures that tests then come to depend on without
    saying so.
    """
    ReasonCode = apps.get_model("inventory", "ReasonCode")

    if not ReasonCode.objects.filter(code="CORR").exists():
        return

    for code, name, description, direction in REPLACEMENTS:
        ReasonCode.objects.get_or_create(
            code=code,
            defaults={"name": name, "description": description, "direction": direction},
        )

    # Retired rather than removed. It stays readable on any adjustment that
    # already used it; it just cannot be chosen for a new one.
    ReasonCode.objects.filter(code="CORR").update(is_active=False)


def restore_correction(apps, schema_editor):
    """Reverse: bring CORR back and drop the pair, if nothing used them."""
    ReasonCode = apps.get_model("inventory", "ReasonCode")

    ReasonCode.objects.filter(code="CORR").update(is_active=True)
    ReasonCode.objects.filter(
        code__in=[code for code, *_ in REPLACEMENTS], adjustments__isnull=True
    ).delete()


class Migration(migrations.Migration):
    dependencies = [("inventory", "0005_inventoryadjustment")]

    operations = [migrations.RunPython(split_correction, restore_correction)]
