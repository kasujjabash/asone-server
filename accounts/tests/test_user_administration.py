"""Creating and managing other people's accounts.

The recurring theme: a lead can do a great deal to someone else's account and
very little to their own, and no route through this API can set a password,
grant Django admin access, or delete an account.
"""

from django.core.cache import cache
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase

from accounts.models import LoginAttempt, User

from .factories import PASSWORD, build_sites, make_user, sign_in


# Sign-in is two steps; locmem lets the tests read the emailed code.
@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class UserAdministrationTests(APITestCase):
    def setUp(self):
        cache.clear()
        self.sites = build_sites()
        self.lead = make_user("sharon", User.Role.PROGRAM_LEAD)
        self.clerk = make_user(
            "julius", User.Role.WAREHOUSE_STAFF, warehouse=self.sites["namayemba"]
        )
        self.client.force_authenticate(self.lead)

    # -- who may reach this at all ---------------------------------------

    def test_warehouse_staff_cannot_list_users(self):
        self.client.force_authenticate(self.clerk)
        response = self.client.get(reverse("accounts:user-list"))
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_finance_cannot_list_users(self):
        """Finance holds Inventory Adj and Financial Reports, not Table Updates."""
        self.client.force_authenticate(make_user("musana", User.Role.FINANCE))
        response = self.client.get(reverse("accounts:user-list"))
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_a_lead_can_list_users(self):
        response = self.client.get(reverse("accounts:user-list"))
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    # -- creating ---------------------------------------------------------

    def create(self, **overrides):
        payload = {
            "first_name": "Joan",
            "last_name": "Adeke",
            "email": "joan@asone.test",
            "role": User.Role.WAREHOUSE_STAFF,
            "warehouse": self.sites["serere"].pk,
            "password": "her-first-passphrase",
        }
        payload.update(overrides)
        return self.client.post(reverse("accounts:user-list"), payload, format="json")

    def test_a_lead_adds_a_user_with_name_email_role_and_password(self):
        response = self.create()

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

        joan = User.objects.get(email="joan@asone.test")
        self.assertEqual(joan.first_name, "Joan")
        self.assertEqual(joan.last_name, "Adeke")
        self.assertEqual(joan.role, User.Role.WAREHOUSE_STAFF)
        self.assertEqual(joan.warehouse, self.sites["serere"])
        self.assertTrue(joan.check_password("her-first-passphrase"))

    def confirm_address(self, email="joan@asone.test"):
        """New accounts must confirm the emailed code before signing in.

        Done directly rather than over HTTP — these tests are about user
        administration, and the confirmation flow has its own file.
        """
        user = User.objects.get(email=email)
        user.email_verified_at = timezone.now()
        user.save(update_fields=["email_verified_at"])

    def test_the_new_user_can_sign_in_with_that_email_and_password(self):
        self.create()
        self.confirm_address()
        self.client.force_authenticate(None)

        response = self.client.post(
            reverse("accounts:login"),
            {"email": "joan@asone.test", "password": "her-first-passphrase"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_they_cannot_sign_in_until_the_address_is_confirmed(self):
        """The password alone is not enough — the address it was created
        against has to be proven first."""
        self.create()
        self.client.force_authenticate(None)

        response = self.client.post(
            reverse("accounts:login"),
            {"email": "joan@asone.test", "password": "her-first-passphrase"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_the_lead_chosen_password_must_pass_django_validators(self):
        """A lead must not be able to set "1234" for a colleague."""
        response = self.create(password="1234")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_first_and_last_name_are_required(self):
        for field in ("first_name", "last_name"):
            with self.subTest(field=field):
                response = self.create(**{field: ""}, email=f"x{field}@asone.test")
                self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_two_accounts_cannot_share_an_email_address(self):
        """It is the login credential — a duplicate would be ambiguous."""
        self.create()
        response = self.create(first_name="Someone", last_name="Else")

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_the_new_user_must_replace_the_password_by_default(self):
        self.create()
        joan = User.objects.get(email="joan@asone.test")
        self.assertTrue(joan.must_change_password)

    def test_a_lead_may_opt_out_of_forcing_a_change(self):
        self.create(must_change_password=False)
        joan = User.objects.get(email="joan@asone.test")
        self.assertFalse(joan.must_change_password)

    def test_a_created_user_must_change_their_password_before_doing_anything(self):
        temporary = "her-first-passphrase"
        self.create(role=User.Role.PROGRAM_LEAD, warehouse=None, password=temporary)

        self.confirm_address()
        self.client.force_authenticate(None)
        # Both steps: a new account still has to pass the emailed sign-in
        # code before it meets the forced password change.
        signed_in = sign_in(self.client, "joan@asone.test", temporary)
        self.assertEqual(signed_in.status_code, status.HTTP_200_OK)
        self.assertTrue(signed_in.data["user"]["must_change_password"])

        token = signed_in.data["access"]
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")

        # Joan is a Program Lead, so this would normally be hers to use.
        blocked = self.client.get(reverse("accounts:user-list"))
        self.assertEqual(blocked.status_code, status.HTTP_403_FORBIDDEN)

        # But she can still read herself and set a password.
        self.assertEqual(
            self.client.get(reverse("accounts:me")).status_code, status.HTTP_200_OK
        )
        changed = self.client.post(
            reverse("accounts:password-change"),
            {"current_password": temporary, "new_password": "her-own-passphrase-42"},
            format="json",
        )
        self.assertEqual(changed.status_code, status.HTTP_200_OK)

        # And afterwards the block is gone.
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {changed.data['access']}")
        self.assertEqual(
            self.client.get(reverse("accounts:user-list")).status_code, status.HTTP_200_OK
        )

    def test_creation_enforces_the_role_and_site_invariant(self):
        """A school user with a warehouse must be refused by the API too."""
        response = self.create(
            role=User.Role.SCHOOL_STAFF, warehouse=self.sites["namayemba"].pk
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_django_admin_access_cannot_be_granted_through_the_api(self):
        self.create(is_staff=True, is_superuser=True)

        joan = User.objects.get(email="joan@asone.test")
        self.assertFalse(joan.is_staff)
        self.assertFalse(joan.is_superuser)

    # -- editing ----------------------------------------------------------

    def test_a_lead_can_edit_name_email_and_role(self):
        response = self.client.patch(
            reverse("accounts:user-detail", args=[self.clerk.pk]),
            {
                "first_name": "Julius",
                "last_name": "Okello",
                "email": "j.okello@asone.test",
                "role": User.Role.FINANCE,
                "warehouse": None,
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        self.clerk.refresh_from_db()
        self.assertEqual(self.clerk.last_name, "Okello")
        self.assertEqual(self.clerk.email, "j.okello@asone.test")
        self.assertEqual(self.clerk.role, User.Role.FINANCE)
        self.assertIsNone(self.clerk.warehouse)

    def test_editing_an_email_to_one_already_in_use_is_refused(self):
        response = self.client.patch(
            reverse("accounts:user-detail", args=[self.clerk.pk]),
            {"email": self.lead.email},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    # -- resetting --------------------------------------------------------

    def test_a_lead_can_set_a_chosen_password_for_someone(self):
        response = self.client.post(
            reverse("accounts:user-set-password", args=[self.clerk.pk]),
            {"new_password": "a-replacement-passphrase"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        self.clerk.refresh_from_db()
        self.assertTrue(self.clerk.check_password("a-replacement-passphrase"))
        self.assertTrue(self.clerk.must_change_password)

    def test_a_lead_set_password_must_pass_django_validators(self):
        response = self.client.post(
            reverse("accounts:user-set-password", args=[self.clerk.pk]),
            {"new_password": "1234"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_omitting_the_password_generates_one(self):
        response = self.client.post(
            reverse("accounts:user-set-password", args=[self.clerk.pk]), {}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        self.clerk.refresh_from_db()
        self.assertTrue(self.clerk.check_password(response.data["password"]))

    def test_setting_a_password_signs_that_user_out(self):
        signed_in = sign_in(self.client, "julius@asone.test", PASSWORD)
        stale_refresh = signed_in.data["refresh"]

        self.client.force_authenticate(self.lead)
        self.client.post(
            reverse("accounts:user-set-password", args=[self.clerk.pk]),
            {"new_password": "a-replacement-passphrase"},
            format="json",
        )

        self.client.force_authenticate(None)
        replayed = self.client.post(
            reverse("accounts:refresh"), {"refresh": stale_refresh}, format="json"
        )
        self.assertEqual(replayed.status_code, status.HTTP_401_UNAUTHORIZED)

    # -- deactivating -----------------------------------------------------

    def test_deactivating_removes_access(self):
        response = self.client.post(
            reverse("accounts:user-deactivate", args=[self.clerk.pk])
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        self.clerk.refresh_from_db()
        self.assertFalse(self.clerk.is_active)

        self.client.force_authenticate(None)
        blocked = self.client.post(
            reverse("accounts:login"),
            {"email": "julius@asone.test", "password": PASSWORD},
            format="json",
        )
        # 403 rather than 401: a deactivated account is told it has no
        # access, the same as one that was never created.
        self.assertEqual(blocked.status_code, status.HTTP_403_FORBIDDEN)

    def test_a_lead_cannot_deactivate_themselves(self):
        """Lockout guard — undoing it would need another lead on site."""
        response = self.client.post(
            reverse("accounts:user-deactivate", args=[self.lead.pk])
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_a_lead_cannot_change_their_own_role(self):
        """Refused even when the change would otherwise be perfectly valid.

        Operations Manager is also an all-locations role, so nothing about
        this payload is malformed — it is blocked because it is their own
        account.
        """
        response = self.client.patch(
            reverse("accounts:user-detail", args=[self.lead.pk]),
            {"role": User.Role.OPERATIONS_MANAGER},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

        self.lead.refresh_from_db()
        self.assertEqual(self.lead.role, User.Role.PROGRAM_LEAD)

    def test_a_lead_can_change_someone_else_role(self):
        response = self.client.patch(
            reverse("accounts:user-detail", args=[self.clerk.pk]),
            {"role": User.Role.FINANCE, "warehouse": None},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        self.clerk.refresh_from_db()
        self.assertEqual(self.clerk.role, User.Role.FINANCE)
        self.assertIsNone(self.clerk.warehouse)

    def test_accounts_cannot_be_deleted(self):
        """The ledger and audit trail point at users. Deactivate instead."""
        response = self.client.delete(
            reverse("accounts:user-detail", args=[self.clerk.pk])
        )
        self.assertEqual(response.status_code, status.HTTP_405_METHOD_NOT_ALLOWED)


class LoginAuditTests(APITestCase):
    def setUp(self):
        cache.clear()
        self.sites = build_sites()
        self.lead = make_user("sharon", User.Role.PROGRAM_LEAD)

    def test_a_successful_sign_in_is_recorded(self):
        self.client.post(
            reverse("accounts:login"),
            {"email": "sharon@asone.test", "password": PASSWORD},
            format="json",
        )
        attempt = LoginAttempt.objects.get()

        self.assertEqual(attempt.email, "sharon@asone.test")
        self.assertTrue(attempt.succeeded)
        self.assertEqual(attempt.user, self.lead)

    def test_a_failed_sign_in_is_recorded(self):
        self.client.post(
            reverse("accounts:login"),
            {"email": "sharon@asone.test", "password": "wrong"},
            format="json",
        )
        attempt = LoginAttempt.objects.get()

        self.assertFalse(attempt.succeeded)
        self.assertEqual(attempt.user, self.lead)

    def test_an_attempt_on_an_unknown_address_is_still_recorded(self):
        """Otherwise guessing at addresses leaves no trace at all."""
        self.client.post(
            reverse("accounts:login"),
            {"email": "intruder@asone.test", "password": "guess"},
            format="json",
        )
        attempt = LoginAttempt.objects.get()

        self.assertEqual(attempt.email, "intruder@asone.test")
        self.assertFalse(attempt.succeeded)
        self.assertIsNone(attempt.user)

    def test_the_audit_trail_is_readable_by_a_lead(self):
        self.client.post(
            reverse("accounts:login"),
            {"email": "sharon@asone.test", "password": PASSWORD},
            format="json",
        )
        self.client.force_authenticate(self.lead)

        response = self.client.get(reverse("accounts:login-attempt-list"))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertGreaterEqual(response.data["count"], 1)

    def test_the_audit_trail_is_not_readable_by_a_clerk(self):
        clerk = make_user(
            "julius", User.Role.WAREHOUSE_STAFF, warehouse=self.sites["namayemba"]
        )
        self.client.force_authenticate(clerk)

        response = self.client.get(reverse("accounts:login-attempt-list"))
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)


class AccountsAreDeactivatedNotDeleted(APITestCase):
    """Accounts carry history, so the verb is removed rather than guarded.

    Only LoginAttempt protects a user today. Without this, an account that
    had never signed in could be erased — and once the stock ledger exists,
    every movement will point at one.
    """

    def setUp(self):
        self.sites = build_sites()
        self.lead = make_user("sharon", User.Role.PROGRAM_LEAD)
        self.clerk = make_user(
            "julius", User.Role.WAREHOUSE_STAFF, warehouse=self.sites["namayemba"]
        )
        self.client.force_authenticate(self.lead)

    def test_a_user_cannot_be_deleted(self):
        response = self.client.delete(
            reverse("accounts:user-detail", args=[self.clerk.pk])
        )

        self.assertEqual(response.status_code, status.HTTP_405_METHOD_NOT_ALLOWED)
        self.assertTrue(User.objects.filter(pk=self.clerk.pk).exists())

    def test_deactivating_is_the_path_instead(self):
        response = self.client.post(
            reverse("accounts:user-deactivate", args=[self.clerk.pk])
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.clerk.refresh_from_db()
        self.assertFalse(self.clerk.is_active)
