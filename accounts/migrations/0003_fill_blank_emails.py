"""Give every account an email address before it becomes the login credential.

Runs before email is made unique. Blank emails would all collide with each
other under a unique constraint, so any account without one is given a
placeholder derived from its username. Those placeholders are not deliverable
addresses — they exist so the constraint can be applied, and a lead is
expected to correct them.
"""

from django.db import migrations


def fill_blank_emails(apps, schema_editor):
    User = apps.get_model("accounts", "User")

    for user in User.objects.filter(email="").order_by("pk"):
        user.email = f"{user.username}@placeholder.invalid"
        user.save(update_fields=["email"])


def unfill(apps, schema_editor):
    """Reverse by clearing only the placeholders this migration created."""
    User = apps.get_model("accounts", "User")
    User.objects.filter(email__endswith="@placeholder.invalid").update(email="")


class Migration(migrations.Migration):
    dependencies = [("accounts", "0002_user_must_change_password_loginattempt")]

    operations = [migrations.RunPython(fill_blank_emails, unfill)]
