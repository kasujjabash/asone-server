"""The eight authentication endpoints, exercised over HTTP.

Several of these are security tests rather than feature tests — they assert
that something is *not* possible. Those are the ones worth keeping when this
file gets trimmed.
"""

from django.core.cache import cache
from django.test import override_settings
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from accounts.models import User

from .factories import PASSWORD, build_sites, make_user, sign_in


# Sign-in is two steps now: a password buys an emailed code. locmem so the
# tests can read the code back out of mail.outbox.
@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class AuthenticationTests(APITestCase):
    def setUp(self):
        # Throttle counters live in the cache and would otherwise carry over
        # between tests in the same process.
        cache.clear()

        self.sites = build_sites()
        self.user = make_user(
            "julius",
            User.Role.WAREHOUSE_STAFF,
            warehouse=self.sites["namayemba"],
            first_name="Julius",
        )

    # -- helpers ---------------------------------------------------------

    def login(self, email="julius@asone.test", password=PASSWORD):
        """The **password step only** — step 1 of 2.

        Returns a challenge, not tokens. Tests about refusals want this;
        tests that need to be signed in want `complete_login()`.
        """
        return self.client.post(
            reverse("accounts:login"),
            {"email": email, "password": password},
            format="json",
        )

    def complete_login(self, email="julius@asone.test", password=PASSWORD):
        """Both steps, returning the response that carries the tokens."""
        return sign_in(self.client, email, password)

    def authenticate(self):
        """Sign in and attach the access token, returning the whole payload."""
        response = self.complete_login()
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {response.data['access']}")
        return response.data

    # -- login -----------------------------------------------------------

    def test_completing_both_steps_returns_tokens_and_the_user(self):
        data = self.complete_login().data

        self.assertIn("access", data)
        self.assertIn("refresh", data)
        self.assertEqual(data["user"]["email"], "julius@asone.test")
        self.assertEqual(data["user"]["role"], User.Role.WAREHOUSE_STAFF)
        self.assertEqual(data["user"]["warehouse"]["name"], "Namayemba")

    def test_login_payload_carries_the_access_summary(self):
        """The frontend draws its navigation from this, so it must be present."""
        access = self.complete_login().data["user"]["access"]

        self.assertEqual(access["scope"], "assigned_warehouse")
        self.assertTrue(access["functions"]["warehouse_receiving_and_shipping"])
        self.assertFalse(access["functions"]["table_updates"])
        self.assertFalse(access["functions"]["inventory_adjustments"])

    def test_login_never_returns_the_password_hash(self):
        self.assertNotIn("password", self.complete_login().data["user"])

    def test_wrong_password_is_rejected(self):
        response = self.login(password="not-it")
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_an_unknown_account_is_told_it_has_no_access(self):
        """**This reverses an earlier decision, deliberately.**

        This test used to assert the opposite — that an unknown address and
        a wrong password were indistinguishable, so that nobody could learn
        which addresses are users here. Bashir asked on 3 September 2026 for
        somebody who was never added to be told so plainly, and that is a
        change of mind about a real trade-off, not a bug fix.

        What is given up: a caller can now discover whether an address has
        an account. What is bought: a teacher who was never added stops
        retyping a password that was never going to work.

        Accepted because this is a closed system of a few dozen accounts
        created by Central Office — nobody self-registers, so the user list
        is not a secret worth keeping. See
        `accounts/services.py::user_with_access`, and revisit it the day
        anybody can sign themselves up.
        """
        unknown = self.login(email="nobody@asone.test", password="whatever")
        wrong = self.login(password="not-it")

        self.assertEqual(unknown.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(wrong.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_deactivated_user_cannot_sign_in(self):
        """Deactivating an account is how AsOne removes someone's access."""
        self.user.is_active = False
        self.user.save(update_fields=["is_active"])

        # 403, not 401: deactivating is how AsOne removes access, and the
        # person is told that plainly rather than left guessing at a password.
        self.assertEqual(self.login().status_code, status.HTTP_403_FORBIDDEN)

    # -- me --------------------------------------------------------------

    def test_me_requires_authentication(self):
        response = self.client.get(reverse("accounts:me"))
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_me_returns_the_signed_in_user(self):
        self.authenticate()
        response = self.client.get(reverse("accounts:me"))

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["email"], "julius@asone.test")

    def test_me_can_update_own_contact_details(self):
        self.authenticate()
        response = self.client.patch(
            reverse("accounts:me"),
            {"first_name": "Julius", "last_name": "Okello", "email": "julius@asone.test"},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.user.refresh_from_db()
        self.assertEqual(self.user.last_name, "Okello")
        self.assertEqual(self.user.email, "julius@asone.test")

    def test_me_cannot_be_used_to_change_role_or_site(self):
        """SECURITY: privilege escalation through the self-service endpoint.

        A warehouse clerk patching themselves into Finance would gain the
        inventory adjustment column. The serializer's field list must ignore
        every one of these.
        """
        self.authenticate()
        response = self.client.patch(
            reverse("accounts:me"),
            {
                "first_name": "Julius",
                "role": User.Role.FINANCE,
                "warehouse": self.sites["serere"].pk,
                "school": self.sites["school_b"].pk,
                "is_staff": True,
                "is_superuser": True,
                "is_active": True,
            },
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.user.refresh_from_db()
        self.assertEqual(self.user.role, User.Role.WAREHOUSE_STAFF)
        self.assertEqual(self.user.warehouse, self.sites["namayemba"])
        self.assertIsNone(self.user.school)
        self.assertFalse(self.user.is_staff)
        self.assertFalse(self.user.is_superuser)

    def test_me_cannot_set_a_password_directly(self):
        self.authenticate()
        self.client.patch(
            reverse("accounts:me"), {"password": "hunter2"}, format="json"
        )

        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password(PASSWORD))

    def test_me_rejects_an_email_already_in_use(self):
        """Email is the login credential, so two accounts cannot share one."""
        make_user("taken@asone.test", User.Role.PROGRAM_LEAD)
        self.authenticate()

        response = self.client.patch(
            reverse("accounts:me"), {"email": "taken@asone.test"}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    # -- refresh and logout ----------------------------------------------

    def test_refresh_returns_a_new_access_token(self):
        refresh = self.complete_login().data["refresh"]

        response = self.client.post(
            reverse("accounts:refresh"), {"refresh": refresh}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("access", response.data)

    def test_a_rotated_refresh_token_cannot_be_reused(self):
        """ROTATE_REFRESH_TOKENS + BLACKLIST_AFTER_ROTATION, proven."""
        refresh = self.complete_login().data["refresh"]
        self.client.post(reverse("accounts:refresh"), {"refresh": refresh}, format="json")

        replayed = self.client.post(
            reverse("accounts:refresh"), {"refresh": refresh}, format="json"
        )
        self.assertEqual(replayed.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_logout_blacklists_the_refresh_token(self):
        payload = self.authenticate()

        response = self.client.post(
            reverse("accounts:logout"), {"refresh": payload["refresh"]}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_205_RESET_CONTENT)

        reused = self.client.post(
            reverse("accounts:refresh"), {"refresh": payload["refresh"]}, format="json"
        )
        self.assertEqual(reused.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_logout_rejects_a_malformed_token(self):
        self.authenticate()
        response = self.client.post(
            reverse("accounts:logout"), {"refresh": "not-a-token"}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    # -- password change --------------------------------------------------

    def test_password_change_requires_the_current_password(self):
        self.authenticate()
        response = self.client.post(
            reverse("accounts:password-change"),
            {"current_password": "wrong", "new_password": "a-brand-new-passphrase"},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password(PASSWORD))

    def test_password_change_applies_django_validators(self):
        self.authenticate()
        response = self.client.post(
            reverse("accounts:password-change"),
            {"current_password": PASSWORD, "new_password": "12345678"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_password_change_succeeds_and_returns_fresh_tokens(self):
        self.authenticate()
        response = self.client.post(
            reverse("accounts:password-change"),
            {"current_password": PASSWORD, "new_password": "a-brand-new-passphrase"},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("access", response.data)
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password("a-brand-new-passphrase"))

    def test_password_change_signs_out_other_sessions(self):
        """A password change should not leave an old device signed in."""
        stale_refresh = self.complete_login().data["refresh"]
        self.authenticate()

        self.client.post(
            reverse("accounts:password-change"),
            {"current_password": PASSWORD, "new_password": "a-brand-new-passphrase"},
            format="json",
        )

        response = self.client.post(
            reverse("accounts:refresh"), {"refresh": stale_refresh}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)
