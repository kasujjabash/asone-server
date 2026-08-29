"""Switch the login credential from username to email address.

Hand-written rather than autodetected: Django could not tell that
LoginAttempt.username became LoginAttempt.email and offered to drop the
column and add a new one, which would have discarded the audit trail. A
rename keeps every existing row.

Order matters here. The unique constraint on User.email can only be applied
once every account has an address, which migration 0003 guarantees.
"""

from django.db import migrations, models

import accounts.managers


class Migration(migrations.Migration):
    dependencies = [("accounts", "0003_fill_blank_emails")]

    operations = [
        # --- the audit trail: rename, keeping its rows -------------------
        migrations.RemoveIndex(
            model_name="loginattempt",
            name="accounts_lo_usernam_cccdc8_idx",
        ),
        migrations.RenameField(
            model_name="loginattempt",
            old_name="username",
            new_name="email",
        ),
        migrations.AlterField(
            model_name="loginattempt",
            name="email",
            field=models.CharField(db_index=True, max_length=254),
        ),
        migrations.AddIndex(
            model_name="loginattempt",
            index=models.Index(fields=["email", "-at"], name="accounts_lo_email_5b78b7_idx"),
        ),
        # --- the user: email becomes the credential ----------------------
        migrations.AlterField(
            model_name="user",
            name="email",
            field=models.EmailField(
                help_text="Used to sign in. Must be unique across all staff.",
                max_length=254,
                unique=True,
                verbose_name="email address",
            ),
        ),
        migrations.RemoveField(
            model_name="user",
            name="username",
        ),
        migrations.AlterModelManagers(
            name="user",
            managers=[("objects", accounts.managers.UserManager())],
        ),
    ]
