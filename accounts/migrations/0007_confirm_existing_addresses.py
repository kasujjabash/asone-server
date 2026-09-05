"""Accounts that existed before email confirmation are treated as confirmed.

Sign-in now requires the address on an account to have been proven with an
emailed code. Accounts created before that requirement have no confirmation
date, so without this migration **every one of them is locked out** — which
is what happened to the development database the moment the field was added.

Marking them confirmed is the honest reading: those accounts were created
deliberately by somebody with lead access, and the new rule is about
addresses nobody has proven, not about people who were already working.

Only ever runs once, and only over rows that exist at the time. Anybody
added afterwards goes through confirmation properly.
"""

from django.db import migrations
from django.utils import timezone


def confirm_existing(apps, schema_editor):
    User = apps.get_model("accounts", "User")
    User.objects.filter(email_verified_at__isnull=True).update(
        email_verified_at=timezone.now()
    )


def unconfirm(apps, schema_editor):
    """Reversing this would lock everybody out again, so it does nothing.

    A migration that can be rolled back into an unusable system is worse
    than one that refuses to undo itself.
    """


class Migration(migrations.Migration):
    dependencies = [("accounts", "0006_email_verification")]

    operations = [migrations.RunPython(confirm_existing, unconfirm)]
