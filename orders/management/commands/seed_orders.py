"""Create a spread of school orders across every status.

The brief tells the frontend team to run `seed_scenario`; no such command
exists, so a screen built against this database shows one cancelled order and
four empty tabs. This fills that gap for the order lifecycle.

Everything goes through `orders.services`, never the ORM directly. That is
the point: an order created by hand can sit in a state the rules would never
have produced — released without a payment reference, picked without stock —
and then the screens are built against something that cannot happen.

Idempotent: it skips if orders already look seeded, so it can be re-run.
"""

import random
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from accounts.models import User
from catalog.models import Kit, School
from orders.models import SchoolOrder
from orders.services import fulfilment, pos, shipping

STUDENTS = [
    "Nakato Grace", "Mukasa John", "Babirye Sarah", "Okello Moses",
    "Nsubuga Paul", "Kisakye Florence", "Ochieng David", "Nakamya Agnes",
    "Waiswa Peter", "Mutesi Proscovia", "Kato Brian", "Namusoke Ruth",
    "Opio Samuel", "Achan Betty",
]


class Command(BaseCommand):
    help = "Create school orders across Hold, Released, Picked, Shipped and Cancelled."

    def add_arguments(self, parser):
        parser.add_argument("--count", type=int, default=14)
        parser.add_argument(
            "--force",
            action="store_true",
            help="Seed even if orders already exist.",
        )

    @transaction.atomic
    def handle(self, *args, **options):
        existing = SchoolOrder.objects.count()
        if existing > 3 and not options["force"]:
            self.stdout.write(
                self.style.WARNING(
                    f"{existing} orders already exist — nothing to do. Use --force."
                )
            )
            return

        clerk = User.objects.filter(role=User.Role.SCHOOL_STAFF, is_active=True).first()
        warehouse_user = User.objects.filter(
            role=User.Role.WAREHOUSE_STAFF, is_active=True, warehouse__isnull=False
        ).first()
        finance = User.objects.filter(role=User.Role.FINANCE, is_active=True).first()

        if not (clerk and warehouse_user and finance):
            self.stderr.write("Need a school clerk, a warehouse user and a finance user.")
            return

        random.seed(20260909)
        today = timezone.localdate()
        made = {"HOLD": 0, "RELEASED": 0, "PICKED": 0, "SHIPPED": 0, "CANCELLED": 0}

        for index in range(options["count"]):
            school = self._school_for(index)
            kit = Kit.objects.filter(school_level=school.level, is_active=True).first()
            if kit is None:
                continue

            order = pos.place_order(
                school=school,
                student_name=STUDENTS[index % len(STUDENTS)],
                order_date=today - timedelta(days=index),
                # The service takes model instances, not ids — it inspects
                # is_active and the kit's components before accepting.
                kits=[{"kit": kit, "quantity": 1}],
                created_by=clerk,
            )
            made["HOLD"] += 1

            # A spread, so every tab and every step of the trail has rows.
            stage = index % 5

            if stage == 0:
                continue  # stays on hold

            if stage == 4:
                pos.cancel_order(order, cancelled_by=clerk, reason="Parent withdrew the student")
                made["HOLD"] -= 1
                made["CANCELLED"] += 1
                continue

            pos.release_order(
                order,
                released_by=finance,
                payment_reference=f"INV-2026-{1000 + index}",
            )
            made["HOLD"] -= 1
            made["RELEASED"] += 1

            if stage == 1:
                continue  # released, awaiting pick

            # Picking needs stock; skip the rest of the chain if short.
            availability = fulfilment.check_availability(order)
            if any(row["shortfall"] > 0 for row in availability):
                continue

            fulfilment.pick_order(order, picked_by=warehouse_user)
            made["RELEASED"] -= 1
            made["PICKED"] += 1

            if stage == 2:
                continue  # picked, awaiting despatch

            shipping.ship_order(order, shipped_by=warehouse_user)
            made["PICKED"] -= 1
            made["SHIPPED"] += 1

        self.stdout.write(self.style.SUCCESS(f"Seeded orders: {made}"))
        self.stdout.write(f"Total in database: {SchoolOrder.objects.count()}")

    def _school_for(self, index):
        schools = list(School.objects.all().order_by("id"))
        return schools[index % len(schools)]
