"""Shared fixtures for the accounts tests.

One place that knows how to build a valid user of each role, so a change to
the role/site invariant does not have to be chased through every test file.
"""

from django.contrib.auth import get_user_model
from django.utils import timezone

from catalog.models import School, TailoringCenter, Warehouse

User = get_user_model()

PASSWORD = "correct-horse-battery-staple"


def build_sites():
    """AsOne's real sites, trimmed to what the tests need.

    Two warehouses and two schools, because most of these tests are about one
    site not being able to see the other.
    """
    idudi = TailoringCenter.objects.create(name="Idudi")
    namayemba = Warehouse.objects.create(name="Namayemba", primary_tailoring_center=idudi)
    serere = Warehouse.objects.create(name="Serere")

    return {
        "namayemba": namayemba,
        "serere": serere,
        "school_a": School.objects.create(
            name="Namayemba PS", level=School.Level.PRIMARY, primary_warehouse=namayemba
        ),
        "school_b": School.objects.create(
            name="Serere HS", level=School.Level.HIGH, primary_warehouse=serere
        ),
    }


def make_user(email, role, *, warehouse=None, school=None, **extra):
    """Create an active user. Sites are passed only where the role allows one.

    `email` may be given as a bare name — "julius" becomes
    "julius@asone.test" — to keep the tests readable.

    The address is marked confirmed by default, because almost every test is
    about something else and an unconfirmed account cannot sign in. Pass
    ``email_verified_at=None`` for a test that is about confirmation itself.
    """
    if "@" not in email:
        email = f"{email}@asone.test"

    extra.setdefault("email_verified_at", timezone.now())

    return User.objects.create_user(
        email=email,
        password=PASSWORD,
        role=role,
        warehouse=warehouse,
        school=school,
        **extra,
    )


def sign_in(client, email, password=PASSWORD):
    """Do both halves of the two-factor sign-in and return the token response.

    Sign-in is two steps since 2FA: a password buys an emailed code, and the
    code buys the tokens. Tests that only care about *being* signed in
    should call this rather than repeating the dance.

    Requires the locmem email backend, so the code can be read back out of
    `mail.outbox` — decorate the test class with::

        @override_settings(
            EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend"
        )
    """
    import re

    from django.core import mail
    from django.urls import reverse

    started = client.post(
        reverse("accounts:login"),
        {"email": email, "password": password},
        format="json",
    )
    if started.status_code != 200:
        # Let the caller assert on the refusal — a wrong password or a
        # deactivated account never reaches the code step.
        return started

    code = re.search(r"\b(\d{6})\b", mail.outbox[-1].body).group(1)
    return client.post(
        reverse("accounts:login-verify"),
        {"challenge": started.data["challenge"], "code": code},
        format="json",
    )
