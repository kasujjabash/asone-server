"""Clear test databases left behind by runs that were killed.

    .venv/bin/python manage.py drop_stale_test_dbs

Each test run now creates its own database — `test_asone_logistics_<pid>` —
so two people can run the suite at once without one dropping the other's out
from under it.

The cost of that is orphans. A run that finishes tidies up after itself; one
that is killed, crashes, or is interrupted with Ctrl-C does not, and its
database sits there forever. A handful is harmless. A few hundred, after a
month of interrupted runs, is a cluttered `\\l` and wasted disk.

**Never touches the real database**, and never touches a test database whose
process is still running — that would do exactly the damage this whole
arrangement exists to prevent.
"""

import os
import re
import signal

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import connection

#: test_<database>_<pid>. The pid group is what tells a stale database from a
#: live one.
PATTERN = re.compile(r"^test_(?P<base>.+)_(?P<pid>\d+)$")


def _process_alive(pid: int) -> bool:
    """Is anything still running under that process id?

    Signal 0 asks the kernel without sending anything. An error means the
    process is gone; permission denied means it exists and belongs to
    somebody else, which still counts as alive.
    """
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class Command(BaseCommand):
    help = "Drop test databases left behind by killed runs. Never touches live ones."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="List what would be dropped and drop nothing.",
        )

    def handle(self, *args, **options):
        base = settings.DATABASES["default"]["NAME"]

        with connection.cursor() as cursor:
            cursor.execute("SELECT datname FROM pg_database WHERE datname LIKE %s",
                           [f"test_{base}%"])
            names = [row[0] for row in cursor.fetchall()]

        stale, live = [], []
        for name in names:
            match = PATTERN.match(name)
            if not match or match.group("base") != base:
                # A test database from before per-run naming, or from another
                # project. Left alone — guessing would be worse than clutter.
                continue
            (live if _process_alive(int(match.group("pid"))) else stale).append(name)

        for name in live:
            self.stdout.write(f"  in use   {name}")

        if not stale:
            self.stdout.write(self.style.SUCCESS("Nothing stale to drop."))
            return

        for name in stale:
            if options["dry_run"]:
                self.stdout.write(f"  would drop  {name}")
                continue

            with connection.cursor() as cursor:
                # Quoted as an identifier: the name came from pg_database and
                # matched the pattern, but building SQL by concatenation is
                # how injection happens even when it cannot here.
                cursor.execute(f'DROP DATABASE IF EXISTS "{name}"')
            self.stdout.write(f"  {self.style.SUCCESS('dropped')}  {name}")

        verb = "would drop" if options["dry_run"] else "dropped"
        self.stdout.write(f"\n{verb} {len(stale)}, left {len(live)} in use.")
