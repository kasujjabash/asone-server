"""User creation, keyed on email.

Django's default manager assumes a `username` field. AsOne staff sign in with
their email address, so this replaces it. Kept in its own module because it is
infrastructure — the interesting rules about a user live on the model.
"""

from django.contrib.auth.models import BaseUserManager


class UserManager(BaseUserManager):
    """Creates users identified by email rather than username."""

    # Lets migrations construct users through this manager.
    use_in_migrations = True

    def _create_user(self, email, password, **extra_fields):
        if not email:
            raise ValueError("An email address is required — it is the login credential.")

        # Lower-cases the domain, so Julius@AsOne.test and julius@asone.test
        # cannot become two accounts.
        email = self.normalize_email(email)

        user = self.model(email=email, **extra_fields)
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_user(self, email, password=None, **extra_fields):
        extra_fields.setdefault("is_staff", False)
        extra_fields.setdefault("is_superuser", False)
        return self._create_user(email, password, **extra_fields)

    def create_superuser(self, email, password=None, **extra_fields):
        extra_fields.setdefault("is_staff", True)
        extra_fields.setdefault("is_superuser", True)

        if extra_fields.get("is_staff") is not True:
            raise ValueError("A superuser must have is_staff=True.")
        if extra_fields.get("is_superuser") is not True:
            raise ValueError("A superuser must have is_superuser=True.")

        return self._create_user(email, password, **extra_fields)
