"""Bring opening stock in through adjustments, the way launch day will.

There is no "add stock" endpoint, deliberately: every movement is a permanent
ledger row tied to a document. So opening stock arrives the same way it will
in real life — a Finance user posts an adjustment with a reason code, and the
ledger records who did it.

Written for development, where a database with no stock makes picking,
shipping and every stock figure impossible to exercise.
"""

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from accounts.models import User
from catalog.models import Sku, Warehouse
from inventory import services
from inventory.models import ReasonCode, StockMovement


class Command(BaseCommand):
    help = "Post opening-stock adjustments for every active SKU at every warehouse."

    def add_arguments(self, parser):
        parser.add_argument("--quantity", type=int, default=400)
        parser.add_argument("--force", action="store_true")

    @transaction.atomic
    def handle(self, *args, **options):
        if StockMovement.objects.count() > 50 and not options["force"]:
            self.stdout.write(self.style.WARNING("Ledger already has rows — use --force."))
            return

        finance = User.objects.filter(role=User.Role.FINANCE, is_active=True).first()
        # CORR_UP is the increase code: a count found higher than the system
        # thought, which is exactly what opening stock is.
        reason = ReasonCode.objects.filter(code="CORR_UP").first()

        if not (finance and reason):
            self.stderr.write("Need a Finance user and the CORR_UP reason code.")
            return

        today = timezone.localdate()
        posted = skipped = 0

        for warehouse in Warehouse.objects.all():
            for sku in Sku.objects.filter(is_active=True):
                try:
                    adjustment = services.create_adjustment(
                        warehouse=warehouse,
                        sku=sku,
                        quantity=options["quantity"],
                        reason_code=reason,
                        created_by=finance,
                        adjustment_date=today,
                        notes="Opening stock at launch",
                    )
                    services.post_adjustment(adjustment, posted_by=finance)
                    posted += 1
                except Exception as exc:
                    # A SKU with no price on the day is refused, by design.
                    skipped += 1
                    if skipped <= 3:
                        self.stdout.write(f"  skipped {sku.number}: {exc}")

        self.stdout.write(self.style.SUCCESS(f"Posted {posted} adjustments, skipped {skipped}."))
