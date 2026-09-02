"""Put AsOne's own reason codes into a database — F13.

    .venv/bin/python manage.py seed_reference_data

**Run this on every new deployment, including production.** Unlike
`seed_demo`, this creates no accounts, no passwords and no invented
activity — only the reason codes AsOne named on p.3 of the pack. So it is
safe to run on a real database, and it does not refuse when DEBUG is off.

## Why this is a command and not a migration

It was a migration first. Seeding master data that way puts rows into every
database including test ones, and 79 existing tests — which quite reasonably
create their own `DMG` or `RET` — collided with the seeded copies. Tests
should declare the data they depend on, not inherit it from a migration
nobody reading the test can see.

So the codes are seeded explicitly, once, by whoever sets up the database.

## Why it matters

Physical count correction (F24) looks up `CORR_UP` and `CORR_DOWN` by code.
Without them it fails, so a deployment that skips this step has a broken
count correction and no obvious reason why. `manage.py check` will not catch
it; nothing will, until a warehouse counts a shelf.

Idempotent and non-destructive: matched on code alone, so a name or
description Central Office has edited is left as they set it, and a code
they retired stays retired.
"""

from django.core.management.base import BaseCommand

from inventory.models import ReasonCode

Direction = ReasonCode.AdjustmentDirection

#: The four AsOne named on p.3 — "Return, Warehouse Transfers, Pick up or
#: Loss, Damaged" — followed by "May be more…", plus the two correction
#: codes F24 depends on by name.
REASON_CODES = [
    (
        "RET",
        "Return",
        "Uniform returned by a school, back into sellable stock.",
        Direction.INCREASE,
    ),
    (
        "XFER",
        "Warehouse transfer",
        "Stock moved between Namayemba and Serere.",
        Direction.DECREASE,
    ),
    (
        "LOSS",
        "Pick up or loss",
        "Stock gone missing, or collected without an order.",
        Direction.DECREASE,
    ),
    (
        "DMG",
        "Damaged",
        "Damaged in transit or in the warehouse, no longer sellable.",
        Direction.DECREASE,
    ),
    (
        "CORR_UP",
        "Inventory correction — count higher",
        "A physical count found more than the system expected.",
        Direction.INCREASE,
    ),
    (
        "CORR_DOWN",
        "Inventory correction — count lower",
        "A physical count found less than the system expected.",
        Direction.DECREASE,
    ),
]

#: The two F24 cannot work without. Reported separately, because a missing
#: `DMG` is an inconvenience and a missing `CORR_UP` is a broken feature.
REQUIRED_BY_COUNT_CORRECTION = ("CORR_UP", "CORR_DOWN")


class Command(BaseCommand):
    help = "Create AsOne's inventory adjustment reason codes. Safe on a real database."

    def handle(self, *args, **options):
        self.stdout.write(self.style.MIGRATE_HEADING("Inventory adjustment reason codes"))

        created_any = False
        for code, name, description, direction in REASON_CODES:
            _, created = ReasonCode.objects.get_or_create(
                code=code,
                defaults={
                    "name": name,
                    "description": description,
                    "direction": direction,
                },
            )
            created_any = created_any or created
            verb = self.style.SUCCESS("created") if created else "exists "
            self.stdout.write(f"  {verb}  {code:10} {name}")

        self._warn_about_count_correction()

        if not created_any:
            self.stdout.write("\nNothing to do — every code was already there.")

    def _warn_about_count_correction(self):
        """Say so plainly if F24 cannot work.

        The codes can exist and still be unusable: Central Office may retire
        one, and a retired code cannot be chosen for a new adjustment.
        """
        unusable = [
            code
            for code in REQUIRED_BY_COUNT_CORRECTION
            if not ReasonCode.objects.filter(code=code, is_active=True).exists()
        ]

        if unusable:
            self.stdout.write("")
            self.stdout.write(
                self.style.WARNING(
                    "Physical count correction needs "
                    f"{' and '.join(unusable)} to be active. Until then it will "
                    "fail. Reactivate them in the admin, or under "
                    "/api/inventory/reason-codes/."
                )
            )
