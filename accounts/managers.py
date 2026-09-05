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
        """The first account on a new system, made at the console.

        **The address is marked confirmed.** Every other account proves its
        address with an emailed code, and this one cannot: there is nobody
        to send it and nobody to receive it — no account exists yet to send
        it from, and on a fresh install the mail server is usually the last
        thing configured.

        It is also unnecessary. Confirmation exists to prove an address that
        *somebody else* typed into a form. Here the person is at a terminal
        on the server, entering their own address, and has already proved far
        more than an email code could.

        Without this, launch day fails completely: the first Program Lead
        cannot sign in to the API, so cannot add anybody, so nothing works.
        Verified by bootstrapping an empty database on 5 September 2026 —
        the account was created, the password was right, and sign-in was
        refused.
        """
        from django.utils import timezone

        extra_fields.setdefault("is_staff", True)
        extra_fields.setdefault("is_superuser", True)
        extra_fields.setdefault("email_verified_at", timezone.now())

        if extra_fields.get("is_staff") is not True:
            raise ValueError("A superuser must have is_staff=True.")
        if extra_fields.get("is_superuser") is not True:
            raise ValueError("A superuser must have is_superuser=True.")

        return self._create_user(email, password, **extra_fields)
