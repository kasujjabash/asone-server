"""Seed production orders worth receiving against.

The receiving screen is a comparison, so it needs orders carrying several
sizes of several garments — a one-line order proves nothing about a
validation matrix. Built through the service layer rather than the ORM, so
the numbering, the price snapshot and the validation are all the real ones.

Tops up to `--count` open orders per warehouse rather than creating blindly,
so running it twice does not double the queue.
"""

from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from accounts.models import User
from catalog.models import Sku, TailoringCenter, Warehouse
from procurement import services
from procurement.models import ProductionOrder
from procurement.models.base import OrderStatus


class Command(BaseCommand):
    help = "Create open production orders with several sizes per garment."

    def add_arguments(self, parser):
        parser.add_argument("--count", type=int, default=4)

    def handle(self, *args, **options):
        wanted = options["count"]

        author = (
            User.objects.filter(role=User.Role.OPERATIONS_MANAGER).first()
            or User.objects.filter(role=User.Role.PROGRAM_LEAD).first()
        )
        if author is None:
            self.stderr.write("No lead account to raise orders as.")
            return

        centers = list(TailoringCenter.objects.all())
        if not centers:
            self.stderr.write("No tailoring centers.")
            return

        catalogue = list(
            Sku.objects.select_related("garment", "size").order_by(
                "garment__name", "size__sort_order"
            )
        )
        today = timezone.localdate()

        for warehouse in Warehouse.objects.all():
            existing = ProductionOrder.objects.filter(
                warehouse=warehouse, status=OrderStatus.OPEN
            ).count()

            for index in range(max(0, wanted - existing)):
                center = centers[index % len(centers)]

                # A window over the catalogue, so each order carries a
                # different mix and the screens are not all identical.
                start = (index * 3) % max(1, len(catalogue) - 7)
                skus = catalogue[start : start + 7]
                if len(skus) < 3:
                    skus = catalogue[:7]

                lines = [
                    {"sku": sku, "quantity": 50 * (position + 3)}
                    for position, sku in enumerate(skus)
                ]

                try:
                    order = services.create_production_order(
                        tailoring_center=center,
                        warehouse=warehouse,
                        order_date=today - timedelta(days=14 + index),
                        due_in_warehouse_date=today + timedelta(days=7 + index),
                        lines=lines,
                        created_by=author,
                    )
                except Exception as exc:  # noqa: BLE001 — a seed reports and moves on
                    self.stderr.write(f"  skipped one for {warehouse.name}: {exc}")
                    continue

                self.stdout.write(
                    f"  {order.number}  {center.name} -> {warehouse.name}  "
                    f"{order.lines.count()} lines"
                )

        self.stdout.write(
            self.style.SUCCESS(
                "Open production orders: "
                f"{ProductionOrder.objects.filter(status=OrderStatus.OPEN).count()}"
            )
        )
