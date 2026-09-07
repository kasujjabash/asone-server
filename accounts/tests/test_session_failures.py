"""Telling a dead server apart from a dead session.

The problem this file protects against, in the user's words: *when the
server is down a user is logged out and they don't know why.*

The server cannot report its own absence — once a request fails to arrive
there is nothing to respond. So the backend's half of the contract is to
make the *other* cases unambiguous, and to give a client something cheap to
ask:

    a request failed with no response
      -> GET /api/health/
           fails    -> the server or the connection is down. Keep the
                       session. Say so. Retry.
           succeeds -> it was authentication. The 401's `code` says which.

    token_expired -> refresh quietly. Do not sign them out.
    token_invalid -> sign them out. This one will never work.

Before this, both came back as `token_not_valid` with the difference buried
in an English sentence, so a client had to match on "Token is expired" to
tell a routine half-hourly expiry from a forged token.
"""

from datetime import timedelta

from django.urls import reverse
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase
from rest_framework_simplejwt.tokens import AccessToken

from accounts.models import User
from accounts.tests.factories import build_sites, make_user
from config.exceptions import TOKEN_EXPIRED, TOKEN_INVALID


class TheHealthCheck(APITestCase):
    """What a client calls when a request comes back with nothing."""

    def test_it_answers_without_a_token(self):
        """The client asking is the one that cannot authenticate."""
        response = self.client.get(reverse("health"))

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["status"], "ok")

    def test_it_reports_the_database(self):
        response = self.client.get(reverse("health"))

        self.assertTrue(response.data["database"])

    def test_a_dead_database_is_degraded_not_ok(self):
        """A server that cannot reach Postgres answers every real request
        with a 500. Calling that healthy sends clients into a retry loop."""
        from unittest import mock

        with mock.patch(
            "config.health.connection.cursor", side_effect=OSError("no postgres")
        ):
            response = self.client.get(reverse("health"))

        self.assertEqual(response.status_code, status.HTTP_503_SERVICE_UNAVAILABLE)
        self.assertFalse(response.data["database"])
        self.assertEqual(response.data["status"], "degraded")


class WhyTheTokenWasRefused(APITestCase):
    def setUp(self):
        build_sites()
        self.user = make_user("julius", User.Role.PROGRAM_LEAD)

    def call_me_with(self, token):
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
        return self.client.get(reverse("accounts:me"))

    def test_an_expired_token_says_expired(self):
        """Routine — they last thirty minutes. Refresh, do not sign out."""
        token = AccessToken.for_user(self.user)
        token.set_exp(from_time=timezone.now() - timedelta(hours=2))

        response = self.call_me_with(token)

        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)
        self.assertEqual(response.data["code"], TOKEN_EXPIRED)

    def test_a_forged_token_says_invalid(self):
        """Never ours, or tampered with. Sign them out."""
        response = self.call_me_with("not-a-real-token")

        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)
        self.assertEqual(response.data["code"], TOKEN_INVALID)

    def test_the_two_are_distinguishable(self):
        """The whole point. Same status, different code — so a client can
        branch without reading English."""
        expired = AccessToken.for_user(self.user)
        expired.set_exp(from_time=timezone.now() - timedelta(hours=2))

        self.assertNotEqual(
            self.call_me_with(expired).data["code"],
            self.call_me_with("not-a-real-token").data["code"],
        )

    def test_no_token_at_all_is_its_own_case(self):
        """Not signed in yet, which is neither expired nor forged."""
        response = self.client.get(reverse("accounts:me"))

        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)
        self.assertEqual(response.data["detail"].code, "not_authenticated")

    def test_an_expired_token_can_still_be_refreshed(self):
        """Proving the advice is actionable: `token_expired` really does
        mean the session is recoverable without signing in again."""
        from rest_framework_simplejwt.tokens import RefreshToken

        token = RefreshToken.for_user(self.user)

        response = self.client.post(
            reverse("accounts:refresh"), {"refresh": str(token)}, format="json"
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("access", response.data)
