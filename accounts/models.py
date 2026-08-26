from django.contrib.auth.models import AbstractUser
from django.core.exceptions import ValidationError
from django.db import models

from .managers import UserManager


class User(AbstractUser):
    """AsOne staff member. Role and site drive what they can see and do.

    Signs in with **email and password**. AbstractUser's `username` is
    removed rather than left unused: two identifiers on one account invites
    the question of which one is authoritative, and a lead filling in a
    creation form should not have to answer it.
    """

    # Removed, not repurposed. USERNAME_FIELD below takes its place.
    username = None

    # The login credential, so it must be unique and is never optional.
    email = models.EmailField(
        "email address",
        unique=True,
        help_text="Used to sign in. Must be unique across all staff.",
    )

    USERNAME_FIELD = "email"

    # What `createsuperuser` prompts for in addition to email and password.
    # Role is here because an account without one is refused by every
    # permission class, which is a confusing way to discover the omission.
    REQUIRED_FIELDS = ["role"]

    objects = UserManager()

    class Role(models.TextChoices):
        PROGRAM_LEAD = "PROGRAM_LEAD", "Program Lead"
        OPERATIONS_MANAGER = "OPERATIONS_MANAGER", "Operations Manager"
        WAREHOUSE_STAFF = "WAREHOUSE_STAFF", "Warehouse Staff"
        SCHOOL_STAFF = "SCHOOL_STAFF", "School Staff"
        FINANCE = "FINANCE", "Finance Department"

    role = models.CharField(max_length=32, choices=Role.choices)

    # Set when an administrator creates the account or resets its password.
    # Until the person chooses their own password, the administrator knows it,
    # which is exactly the shared-password situation AsOne asked us to prevent
    # (p.9: "There can be no sharing of Passwords"). Enforced by
    # accounts.permissions.PasswordChangeNotPending.
    must_change_password = models.BooleanField(
        default=False,
        help_text="Blocks everything except viewing your own account and setting a new password.",
    )

    warehouse = models.ForeignKey(
        "catalog.Warehouse",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="staff",
    )
    school = models.ForeignKey(
        "catalog.School",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="staff",
    )

    # Roles the access matrix scopes to "All Locations". They are attached to
    # no single site, so a warehouse or school on them would be meaningless.
    ALL_SITE_ROLES = (Role.PROGRAM_LEAD, Role.OPERATIONS_MANAGER, Role.FINANCE)

    #: Which site field each role must carry, if any. The single source for
    #: this rule: clean() enforces it, and the roles endpoint publishes it so
    #: the user-creation form knows which site picker to show.
    SITE_FIELD_BY_ROLE = {
        Role.WAREHOUSE_STAFF: "warehouse",
        Role.SCHOOL_STAFF: "school",
    }

    @classmethod
    def required_site_field(cls, role):
        """"warehouse", "school", or None for the all-locations roles."""
        return cls.SITE_FIELD_BY_ROLE.get(role)

    def clean(self):
        """Keep role and site consistent.

        AsOne's user table (p.3) is User Name / Role / Site, with one site per
        user. A school user carrying a warehouse — or a warehouse user with no
        warehouse — would make scope_to_user_site() silently return nothing,
        which reads as "there is no stock" rather than as a broken account.
        Caught here so the admin refuses to save it in the first place.
        """
        super().clean()
        errors = {}

        if self.role == self.Role.WAREHOUSE_STAFF:
            if self.warehouse is None:
                errors["warehouse"] = "Warehouse staff must be assigned a warehouse."
            if self.school is not None:
                errors["school"] = "Warehouse staff cannot be assigned a school."

        elif self.role == self.Role.SCHOOL_STAFF:
            if self.school is None:
                errors["school"] = "School staff must be assigned a school."
            if self.warehouse is not None:
                errors["warehouse"] = "School staff cannot be assigned a warehouse."

        elif self.role in self.ALL_SITE_ROLES:
            if self.warehouse is not None:
                errors["warehouse"] = (
                    f"{self.get_role_display()} covers all locations and takes no warehouse."
                )
            if self.school is not None:
                errors["school"] = (
                    f"{self.get_role_display()} covers all locations and takes no school."
                )

        if errors:
            raise ValidationError(errors)

    def __str__(self):
        return f"{self.get_full_name() or self.email} ({self.get_role_display()})"


class LoginAttempt(models.Model):
    """An audit row for every sign-in attempt, successful or not.

    AsOne asked for user names captured on transactions and available in
    audit trails (p.9). Failed attempts are recorded too: a run of failures
    against one account is the signal that someone is guessing, and it is
    only visible if the failures are kept.

    Append-only. Nothing in the application updates or deletes these rows.
    """

    # Exactly what was typed into the email box, kept even when no such
    # account exists — otherwise an attack on a guessed address leaves no
    # trace at all. A plain CharField, not EmailField: a failed attempt is
    # often not a valid address, and the audit trail should still hold it.
    email = models.CharField(max_length=254, db_index=True)

    # PROTECT so an account cannot be deleted out from under its own audit
    # trail. AsOne deactivates staff rather than deleting them anyway.
    user = models.ForeignKey(
        User,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="login_attempts",
    )

    succeeded = models.BooleanField()
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.CharField(max_length=300, blank=True)
    at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-at"]
        indexes = [models.Index(fields=["email", "-at"])]

    def __str__(self):
        outcome = "succeeded" if self.succeeded else "failed"
        return f"{self.email} {outcome} at {self.at:%Y-%m-%d %H:%M}"
