"""Populate a development database with AsOne's real sites and one user per role.

    .venv/bin/python manage.py seed_demo

Idempotent — run it as often as you like. It creates nothing that does not
already exist and never overwrites a password you have changed.

It lives in `accounts` because its purpose is the users; the sites exist so
those users have somewhere to belong.
"""

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from accounts.models import User
from catalog.models import School, TailoringCenter, Warehouse

#: Known password, printed to the terminal. This is exactly why the command
#: refuses to run outside DEBUG.
DEMO_PASSWORD = "asone-demo-2027"

TAILORING_CENTERS = ["Idudi", "Serere", "Rwanyabihuka"]

# Namayemba also hosts AsOne's central office.
WAREHOUSES = [
    ("Namayemba", "Idudi"),
    ("Serere", "Serere"),
]

SCHOOLS = [
    ("Namayemba Primary School", School.Level.PRIMARY, "Namayemba"),
    ("Bugiri High School", School.Level.HIGH, "Namayemba"),
    ("Serere Primary School", School.Level.PRIMARY, "Serere"),
    ("Serere High School", School.Level.HIGH, "Serere"),
]

# email, first name, last name, role, warehouse, school
DEMO_USERS = [
    ("sharon@asone.test", "Sharon", "Nakato", User.Role.PROGRAM_LEAD, None, None),
    ("andrew@asone.test", "Andrew", "Mugisha", User.Role.OPERATIONS_MANAGER, None, None),
    ("musana@asone.test", "Musana", "Kato", User.Role.FINANCE, None, None),
    ("julius@asone.test", "Julius", "Okello", User.Role.WAREHOUSE_STAFF, "Namayemba", None),
    ("joan@asone.test", "Joan", "Adeke", User.Role.WAREHOUSE_STAFF, "Serere", None),
    ("chrisis@asone.test", "Chrisis", "Nabirye", User.Role.SCHOOL_STAFF, None, "Namayemba Primary School"),
    ("peter@asone.test", "Peter", "Owor", User.Role.SCHOOL_STAFF, None, "Serere High School"),
]


class Command(BaseCommand):
    help = "Create AsOne's sites and one demo user per role. Development only."

    def add_arguments(self, parser):
        parser.add_argument(
            "--force",
            action="store_true",
            help="Run even with DEBUG off. You almost certainly do not want this.",
        )
        parser.add_argument(
            "--reset-passwords",
            action="store_true",
            help=(
                "Put every demo account back on the shared demo password. "
                "Use after testing a password change has left you locked out."
            ),
        )

    @transaction.atomic
    def handle(self, *args, **options):
        if not settings.DEBUG and not options["force"]:
            raise CommandError(
                "seed_demo creates accounts with a published password and is "
                "refusing to run with DEBUG off. Pass --force only if you are "
                "certain this is not a real database."
            )

        centers = self._seed(
            "Tailoring Centers",
            TAILORING_CENTERS,
            lambda name: TailoringCenter.objects.get_or_create(name=name),
        )

        warehouses = self._seed(
            "Warehouses",
            WAREHOUSES,
            lambda row: Warehouse.objects.get_or_create(
                name=row[0],
                defaults={"primary_tailoring_center": centers[row[1]]},
            ),
            key=lambda row: row[0],
        )

        self._seed(
            "Schools",
            SCHOOLS,
            lambda row: School.objects.get_or_create(
                name=row[0],
                defaults={"level": row[1], "primary_warehouse": warehouses[row[2]]},
            ),
            key=lambda row: row[0],
        )

        self._seed_users(warehouses)

        if options["reset_passwords"]:
            self._reset_passwords()

        self._report()

    # -- helpers ---------------------------------------------------------

    def _seed(self, label, rows, create, key=lambda row: row):
        """Run `create` over `rows`, reporting what was new. Returns a name map."""
        self.stdout.write(self.style.MIGRATE_HEADING(label))
        made = {}
        for row in rows:
            obj, created = create(row)
            made[key(row)] = obj
            verb = self.style.SUCCESS("created") if created else "exists "
            self.stdout.write(f"  {verb}  {obj}")
        return made

    def _seed_users(self, warehouses):
        self.stdout.write(self.style.MIGRATE_HEADING("Users"))
        schools = {s.name: s for s in School.objects.all()}

        for email, first_name, last_name, role, warehouse_name, school_name in DEMO_USERS:
            if User.objects.filter(email__iexact=email).exists():
                self.stdout.write(f"  exists   {email}")
                continue

            user = User(
                email=email,
                first_name=first_name,
                last_name=last_name,
                role=role,
                warehouse=warehouses.get(warehouse_name) if warehouse_name else None,
                school=schools.get(school_name) if school_name else None,
            )
            user.set_password(DEMO_PASSWORD)
            # Run the role/site invariant rather than trusting the table above.
            user.full_clean(exclude=["password"])
            user.save()
            self.stdout.write(f"  {self.style.SUCCESS('created')}  {user}")

    def _reset_passwords(self):
        """Put the demo accounts back on the shared password.

        Testing a password change leaves the account on whatever it was
        changed to, which is correct behaviour and inconvenient the next time
        you want to sign in. This undoes it.

        Only touches the accounts this command creates — a real account that
        happens to share the database is left alone.
        """
        self.stdout.write(self.style.MIGRATE_HEADING("Resetting demo passwords"))

        demo_emails = [row[0] for row in DEMO_USERS]
        for user in User.objects.filter(email__in=demo_emails).order_by("email"):
            user.set_password(DEMO_PASSWORD)
            user.must_change_password = False
            user.save(update_fields=["password", "must_change_password"])
            self.stdout.write(f"  {self.style.SUCCESS('reset')}    {user.email}")

    def _report(self):
        self.stdout.write("")
        self.stdout.write(self.style.WARNING(f"All demo users share the password: {DEMO_PASSWORD}"))
        self.stdout.write("")
        self.stdout.write("Sign in with the email address, not a username.")
        self.stdout.write("")
        self.stdout.write("Try these against each other:")
        self.stdout.write("  julius@  (Namayemba) vs joan@ (Serere)    — must not see each other's site")
        self.stdout.write("  chrisis@ (school)    vs musana@ (Finance) — different matrix columns entirely")
