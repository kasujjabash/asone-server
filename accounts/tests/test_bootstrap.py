"""Getting a brand new system started — the first account.

There is a chicken-and-egg problem at launch: a lead adds everybody else,
but nobody can add the lead. `createsuperuser` at the console is the way in.

**This file exists because that was broken.** Email confirmation was added,
and nothing exempted the bootstrap account — so on an empty database the
first Program Lead was created successfully, held the right password, and
was refused at sign-in with a code that had never been sent and could not
be. Launch day would have failed completely. Found by bootstrapping an empty
database on 5 September 2026, not by a test.
"""

from django.core import mail
from django.core.cache import cache
from django.test import override_settings
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from accounts.models import User
from accounts.tests.factories import sign_in

FIRST_PASSWORD = "a-real-launch-passphrase"


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class TheFirstAccountOnAnEmptySystem(APITestCase):
    def setUp(self):
        cache.clear()
        mail.outbox.clear()
        self.first = User.objects.create_superuser(
            email="bashir@era92.com",
            password=FIRST_PASSWORD,
            role=User.Role.PROGRAM_LEAD,
        )

    def test_the_address_is_treated_as_confirmed(self):
        """Nobody could send a code, and nobody needs to — the person is at
        a terminal on the server entering their own address."""
        self.assertTrue(self.first.email_is_verified)

    def test_they_can_sign_in_to_the_api(self):
        """The whole of launch day depends on this."""
        response = sign_in(self.client, "bashir@era92.com", FIRST_PASSWORD)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("access", response.data)

    def test_they_are_not_forced_to_change_the_password_they_just_chose(self):
        self.assertFalse(self.first.must_change_password)

    def test_they_can_then_add_everybody_else(self):
        """The point of the bootstrap: one account that can create the rest."""
        self.client.force_authenticate(self.first)

        response = self.client.post(
            reverse("accounts:user-list"),
            {
                "first_name": "Sharon",
                "last_name": "Nakato",
                "email": "sharon@asone.test",
                "role": User.Role.OPERATIONS_MANAGER,
            },
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertTrue(response.data["password"])

    def test_the_people_they_add_do_get_a_confirmation_code(self):
        """Only the bootstrap account skips it. Everybody else proves the
        address somebody else typed for them."""
        self.client.force_authenticate(self.first)
        self.client.post(
            reverse("accounts:user-list"),
            {
                "first_name": "Sharon",
                "last_name": "Nakato",
                "email": "sharon@asone.test",
                "role": User.Role.OPERATIONS_MANAGER,
            },
            format="json",
        )

        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ["sharon@asone.test"])
        self.assertFalse(
            User.objects.get(email="sharon@asone.test").email_is_verified
        )
