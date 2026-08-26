"""Login rate limits.

The behaviour that matters here is not "attackers get blocked" — it is that
a whole warehouse behind one shared connection does not get blocked with
them. Every AsOne site is a single rural internet connection, so colleagues
are indistinguishable by IP address.
"""

from django.core.cache import cache
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from accounts.models import User

from .factories import PASSWORD, build_sites, make_user


class LoginThrottleTests(APITestCase):
    def setUp(self):
        cache.clear()
        self.sites = build_sites()
        self.julius = make_user(
            "julius", User.Role.WAREHOUSE_STAFF, warehouse=self.sites["namayemba"]
        )
        self.joan = make_user(
            "joan", User.Role.WAREHOUSE_STAFF, warehouse=self.sites["namayemba"]
        )

    def attempt(self, email, password="wrong"):
        if "@" not in email and email:
            email = f"{email}@asone.test"
        return self.client.post(
            reverse("accounts:login"),
            {"email": email, "password": password},
            format="json",
        )

    def test_repeated_failures_against_one_account_are_throttled(self):
        statuses = [self.attempt("julius").status_code for _ in range(12)]

        self.assertIn(status.HTTP_429_TOO_MANY_REQUESTS, statuses)

    def test_one_colleague_being_throttled_does_not_lock_out_another(self):
        """The point of keying on the email address rather than the IP.

        Julius exhausts his allowance. Joan, at the same warehouse and
        therefore the same IP address, must still be able to sign in.
        """
        for _ in range(12):
            self.attempt("julius")

        self.assertEqual(
            self.attempt("julius").status_code, status.HTTP_429_TOO_MANY_REQUESTS
        )

        response = self.attempt("joan", password=PASSWORD)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_a_whole_site_can_sign_in_at_the_start_of_the_day(self):
        """Ten staff on one connection, all with correct passwords."""
        for index in range(10):
            user = make_user(
                f"clerk{index}",
                User.Role.WAREHOUSE_STAFF,
                warehouse=self.sites["namayemba"],
            )
            response = self.attempt(user.email, password=PASSWORD)
            self.assertEqual(
                response.status_code,
                status.HTTP_200_OK,
                msg=f"{user.email} was blocked by a colleague's sign-in",
            )

    def test_address_spraying_from_one_connection_is_still_caught(self):
        """The per-IP backstop. Many different addresses, one caller."""
        statuses = [self.attempt(f"target{i}").status_code for i in range(80)]

        self.assertIn(status.HTTP_429_TOO_MANY_REQUESTS, statuses)

    def test_a_request_with_no_email_falls_back_to_the_caller(self):
        """Empty payloads must not be a free pass around the per-email key."""
        statuses = [self.attempt("").status_code for _ in range(12)]

        self.assertIn(status.HTTP_429_TOO_MANY_REQUESTS, statuses)
